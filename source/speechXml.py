# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2016-2023 NV Access Limited, Leonard de Ruijter
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

"""Utilities for converting NVDA speech sequences to XML and vice versa.
Several synthesizers accept XML, either SSML or their own schemas.
L{SpeechXmlConverter} is the base class for conversion to XML.
You can subclass this to support specific XML schemas.
L{SsmlConverter} is an implementation for conversion to SSML.
"""

import re
from collections import OrderedDict, namedtuple
from collections.abc import Callable, Generator
from xml.parsers import expat

import textUtils
from logHandler import log
from speech.commands import (
	BreakCommand,
	CallbackCommand,
	CharacterModeCommand,
	LangChangeCommand,
	PitchCommand,
	RateCommand,
	SpeechCommand,
	VolumeCommand,
)
from speech.types import SpeechSequence

XML_ESCAPES = {
	0x3C: "&lt;",  # <
	0x3E: "&gt;",  # >
	0x26: "&amp;",  # &
	0x22: "&quot;",  # "
}


# Regular expression to replace invalid XML characters.
# Based on http://stackoverflow.com/a/22273639
def _buildInvalidXmlRegexp():
	# Ranges of invalid characters.
	# Both start and end are inclusive; i.e. they are both themselves considered invalid.
	ranges = (
		(0x00, 0x08),
		(0x0B, 0x0C),
		(0x0E, 0x1F),
		(0x7F, 0x84),
		(0x86, 0x9F),
		(0xFDD0, 0xFDDF),
		(0xFFFE, 0xFFFF),
	)
	rangeExprs = ["%s-%s" % (chr(start), chr(end)) for start, end in ranges]
	leadingSurrogate = "[\ud800-\udbff]"
	trailingSurrogate = "[\udc00-\udfff]"
	return re.compile(
		(
			# These ranges of characters are invalid.
			"[{ranges}]"
			# Leading Unicode surrogate is invalid if not followed by trailing surrogate.
			"|{leading}(?!{trailing})"
			# Trailing surrogate is invalid if not preceded by a leading surrogate.
			"|(?<!{leading}){trailing}"
		).format(
			ranges="".join(rangeExprs),
			leading=leadingSurrogate,
			trailing=trailingSurrogate,
		),
	)


RE_INVALID_XML_CHARS = _buildInvalidXmlRegexp()
RE_TIME_MS = re.compile(r"^(?P<time>\d+)ms$", re.IGNORECASE)
RE_PERCENTAGE = re.compile(r"^(?P<percentage>\d+(\.\d+)?)%$")
REPLACEMENT_CHAR = textUtils.REPLACEMENT_CHAR


def toXmlLang(nvdaLang: str) -> str:
	"""Convert an NVDA language to an XML language."""
	return nvdaLang.replace("_", "-")


def toNvdaLang(xmlLang: str) -> str:
	"""Convert an XML language to an NVDA language."""
	return xmlLang.replace("-", "_")


#: An XMLBalancer command to enclose the entire output in a tag.
#: This must be the first command.
EncloseAllCommand = namedtuple("EncloseAllCommand", ("tag", "attrs"))
#: An XMLBalancer command to set a tag attribute to a given value for subsequent output.
#: This attribute will be output with this value until a L{DelAttrCommand}.
SetAttrCommand = namedtuple("SetAttrCommand", ("tag", "attr", "val"))
#: An XmlBalancer command to remove a tag attribute for subsequent output.
#: If the tag has no remaining attributes, it will not be produced henceforth.
DelAttrCommand = namedtuple("DelAttrCommand", ("tag", "attr"))
#: An XmlBalancer command to directly enclose all text henceforth in a tag.
#: That is, the tag must always be the inner most tag.
#: This will occur until a L{StopEnclosingTextCommand}.
EncloseTextCommand = namedtuple("EncloseTextCommand", ("tag", "attrs"))
#: An XMLBalancer command to stop directly enclosing text henceforth in a tag.
StopEnclosingTextCommand = namedtuple("StopEnclosingTextCommand", ())
#: An XmlBalancer command to output a stand-alone tag.
#: That is, it will not enclose subsequent output.
StandAloneTagCommand = namedtuple("StandAloneTagCommand", ("tag", "attrs", "content"))


def _escapeXml(text):
	text = text.translate(XML_ESCAPES)
	text = RE_INVALID_XML_CHARS.sub(REPLACEMENT_CHAR, text)
	return text


class XmlBalancer:
	"""Generates balanced XML given a set of commands.
	NVDA speech sequences are linear, but XML is hierarchical, which makes conversion challenging.
	For example, a speech sequence might change the pitch, then change the volume, then reset the pitch to default.
	In XML, resetting to default generally requires closing the tag, but that also requires closing the outer tag.
	This class transparently handles these issues, balancing the XML as appropriate.
	To use, create an instance and call the L{generateXml} method.
	"""

	def __init__(self):
		#: The converted output as it is built.
		self._out = []
		#: A stack of open tags which enclose the entire output.
		self._enclosingAllTags = []
		#: Whether any tags have changed since last time they were output.
		self._tagsChanged = False
		#: A stack of currently open tags (excluding tags which enclose the entire output).
		self._openTags = []
		#: Current tags and their attributes.
		self._tags = OrderedDict()
		#: A tag (and its attributes) which should directly enclose all text henceforth.
		self._tagEnclosingText = (None, None)

	def _text(self, text):
		tag, attrs = self._tagEnclosingText
		if tag:
			self._openTag(tag, attrs)
		self._out.append(_escapeXml(text))
		if tag:
			self._closeTag(tag)

	def _openTag(self, tag, attrs, empty=False):
		self._out.append("<%s" % tag)
		for attr, val in attrs.items():
			self._out.append(' %s="' % attr)
			# Attribute values could be ints, floats etc, not just strings.
			# Therefore coerce the value to a string, as well as escaping xml characters.
			self._out.append(_escapeXml(str(val)))
			self._out.append('"')
		self._out.append("/>" if empty else ">")

	def _closeTag(self, tag):
		self._out.append("</%s>" % tag)

	def _setAttr(self, tag, attr, val):
		attrs = self._tags.get(tag)
		if not attrs:
			attrs = self._tags[tag] = OrderedDict()
		if attrs.get(attr) != val:
			attrs[attr] = val
			self._tagsChanged = True

	def _delAttr(self, tag, attr):
		attrs = self._tags.get(tag)
		if not attrs:
			return
		if attr not in attrs:
			return
		del attrs[attr]
		if not attrs:
			del self._tags[tag]
		self._tagsChanged = True

	def _outputTags(self):
		if not self._tagsChanged:
			return
		# Just close all open tags and reopen any existing or new ones.
		for tag in reversed(self._openTags):
			self._closeTag(tag)
		del self._openTags[:]
		for tag, attrs in self._tags.items():
			self._openTag(tag, attrs)
			self._openTags.append(tag)
		self._tagsChanged = False

	def generateXml(self, commands) -> str:
		"""Generate XML from a sequence of balancer commands and text."""
		for command in commands:
			if isinstance(command, str):
				self._outputTags()
				self._text(command)
			elif isinstance(command, EncloseAllCommand):
				self._openTag(command.tag, command.attrs)
				self._enclosingAllTags.append(command.tag)
			elif isinstance(command, SetAttrCommand):
				self._setAttr(command.tag, command.attr, command.val)
			elif isinstance(command, DelAttrCommand):
				self._delAttr(command.tag, command.attr)
			elif isinstance(command, EncloseTextCommand):
				self._tagEnclosingText = (command.tag, command.attrs)
			elif isinstance(command, StopEnclosingTextCommand):
				self._tagEnclosingText = (None, None)
			elif isinstance(command, StandAloneTagCommand):
				self._outputTags()
				self._openTag(command.tag, command.attrs, empty=not command.content)
				if command.content:
					self._text(command.content)
					self._closeTag(command.tag)
		# Close any open tags.
		for tag in reversed(self._openTags):
			self._closeTag(tag)
		for tag in self._enclosingAllTags:
			self._closeTag(tag)
		return "".join(self._out)


class SpeechXmlConverter:
	"""Base class for conversion of NVDA speech sequences to XML.
	This class converts an NVDA speech sequence into XmlBalancer commands
	which can then be passed to L{XmlBalancer} to produce correct XML.

	The L{generateBalancerCommands} method takes a speech sequence
	and produces corresponding XmlBalancer commands.
	For convenience, callers can call L{convertToXml} with a speech sequence
	to generate XML using L{XmlBalancer}.

	Subclasses implement specific XML schemas by implementing methods which convert each speech command.
	The method for a speech command should be named with the prefix "convert" followed by the command's class name.
	For example, the handler for C{IndexCommand} should be named C{convertIndexCommand}.
	These methods receive the L{SpeechCommand} instance as their only argument.
	They should return an appropriate XmlBalancer command.
	Subclasses may wish to extend L{generateBalancerCommands}
	to produce additional XmlBalancer commands at the start or end;
	e.g. to add an L{EncloseAllCommand} at the start.
	"""

	def generateBalancerCommands(self, speechSequence):
		"""Generate appropriate XmlBalancer commands for a given speech sequence.
		@rtype: generator
		"""
		for item in speechSequence:
			if isinstance(item, str):
				yield item
			elif isinstance(item, SpeechCommand):
				name = type(item).__name__
				# For example: self.convertIndexCommand
				func = getattr(self, "convert%s" % name, None)
				if not func:
					log.debugWarning("Unsupported command: %s" % item)
					return
				command = func(item)
				if command is not None:
					yield command
			else:
				log.error("Unknown speech: %r" % item)

	def convertToXml(self, speechSequence):
		"""Convenience method to convert a speech sequence to XML using L{XmlBalancer}."""
		bal = XmlBalancer()
		balCommands = self.generateBalancerCommands(speechSequence)
		return bal.generateXml(balCommands)


class SsmlConverter(SpeechXmlConverter):
	"""Converts an NVDA speech sequence to SSML."""

	def __init__(self, defaultLanguage: str):
		self.defaultLanguage = toXmlLang(defaultLanguage)

	def generateBalancerCommands(self, speechSequence):
		attrs = OrderedDict(
			(
				("version", "1.0"),
				("xmlns", "http://www.w3.org/2001/10/synthesis"),
				("xml:lang", self.defaultLanguage),
			),
		)
		yield EncloseAllCommand("speak", attrs)
		for command in super(SsmlConverter, self).generateBalancerCommands(speechSequence):
			yield command

	def convertIndexCommand(self, command):
		return StandAloneTagCommand("mark", {"name": command.index}, None)

	def convertCharacterModeCommand(self, command):
		if command.state:
			return EncloseTextCommand("say-as", {"interpret-as": "characters"})
		else:
			return StopEnclosingTextCommand()

	def convertLangChangeCommand(self, command: LangChangeCommand) -> SetAttrCommand:
		lang = command.lang or self.defaultLanguage
		lang = toXmlLang(lang)
		return SetAttrCommand("voice", "xml:lang", lang)

	def convertBreakCommand(self, command):
		return StandAloneTagCommand("break", {"time": "%dms" % command.time}, None)

	def _convertProsody(self, command, attr):
		if command.multiplier == 1:
			# Returning to normal.
			return DelAttrCommand("prosody", attr)
		else:
			return SetAttrCommand(
				"prosody",
				attr,
				"%d%%" % int(command.multiplier * 100),
			)

	def convertPitchCommand(self, command):
		return self._convertProsody(command, "pitch")

	def convertRateCommand(self, command):
		return self._convertProsody(command, "rate")

	def convertVolumeCommand(self, command):
		return self._convertProsody(command, "volume")

	def convertPhonemeCommand(self, command):
		return StandAloneTagCommand("phoneme", {"alphabet": "ipa", "ph": command.ipa}, command.text)


class SpeechXmlParser:
	"""Base class for parsing of NVDA speech sequences from XML.
	This class converts XML to an NVDA speech sequence.

	Callers can call L{convertFromXml} with XML to generate a speech sequence.

	Subclasses implement specific XML schemas by implementing generators which convert each XML tag supported.
	The method for a tag should be named with the prefix "parse" followed by the tag.
	For example, the handler for <volume /> should be named C{parseVolume}.
	These generators receive an optional dictionary containing the attributes and values.
	When the attributes value is None, it is a closing tag.
	They should yield one or more appropriate SPeechCommand instances.
	"""

	_speechSequence: SpeechSequence

	def _elementHandler(self, tagName: str, attrs: dict | None = None):
		processedTagName = "".join(tagName.title().split("-"))
		funcName = f"parse{processedTagName}"
		if (func := getattr(self, funcName, None)) is None:
			log.debugWarning(f"Unsupported tag: {tagName}")
			return
		for command in func(attrs):
			# If the last command in the sequence is of the same type, we can remove it.
			if self._speechSequence and type(self._speechSequence[-1]) is type(command):
				self._speechSequence.pop()
			# Look up the previous command of the same class, if any.
			# If the last instance of this command in the sequence is equal to this command, we don't have to add it.
			prevCommand = next((c for c in reversed(self._speechSequence) if type(c) is type(command)), None)
			if prevCommand != command:
				self._speechSequence.append(command)

	def convertFromXml(self, xml: str) -> SpeechSequence:
		"""Convert XML to a speech sequence."""
		self._speechSequence = SpeechSequence()
		parser = expat.ParserCreate("utf-8")
		parser.StartElementHandler = parser.EndElementHandler = self._elementHandler
		parser.CharacterDataHandler = self._speechSequence.append
		try:
			parser.Parse(xml)
		except Exception as e:
			raise ValueError(f"XML: {xml}") from e
		return self._speechSequence


ParseGeneratorT = Generator[SpeechCommand, None, None]
ParseFuncT = Callable[[dict[str, str] | None], ParseGeneratorT]
MarkCallbackT = Callable[[str], None]


class SsmlParser(SpeechXmlParser):
	"""Parses SSML into an NVDA speech sequence."""

	def __init__(self, markCallback: MarkCallbackT | None = None):
		"""Constructor.

		:param markCallback: An optional callback called for every mark command in the SSML.
			The mark command in the SSML will be translated to a CallbackCommand instance.
		"""
		self._markCallback = markCallback

	def parseSayAs(self, attrs: dict[str, str] | None) -> ParseGeneratorT:
		state = attrs is not None and attrs.get("interpret-as") == "characters"
		yield CharacterModeCommand(state)

	def parseVoice(self, attrs: dict[str, str] | None) -> ParseGeneratorT:
		if attrs is None:
			return
		if (xmlLang := attrs.get("xml:lang")) is None:
			return
		yield LangChangeCommand(toNvdaLang(xmlLang))

	def parseBreak(self, attrs: dict[str, str] | None) -> ParseGeneratorT:
		if attrs is None or "time" not in attrs:
			return
		if (time := RE_TIME_MS.match(attrs["time"])) is None:
			log.debugWarning(f"Unknown attributes for break tag: {attrs}")
			return
		yield BreakCommand(int(time.group("time")))

	_cachedProsodyAttrs: list[dict]

	def parseProsody(self, attrs: dict[str, str] | None) -> ParseGeneratorT:
		if isOpenTag := attrs is not None:
			self._cachedProsodyAttrs.append(attrs)
		else:  # attrs is None
			# Pop the attrs from the cache so we can add commands to reset them.
			attrs = self._cachedProsodyAttrs.pop()
		for attr, val in attrs.items():
			if (percentage := RE_PERCENTAGE.match(val)) is None:
				log.debugWarning(f"Attribute {attr!r} for prosody tag has unparseable value: {val!r}")
				continue
			multiplier = float(percentage.group("percentage")) / 100 if isOpenTag else 1
			match attr:
				case "pitch":
					yield PitchCommand(multiplier=multiplier)
				case "volume":
					yield VolumeCommand(multiplier=multiplier)
				case "rate":
					yield RateCommand(multiplier=multiplier)
				case _:
					log.debugWarning(f"Unknown prosody attribute: {attr!r}")
					continue

	def parseSpeak(self, attrs: dict[str, str] | None) -> ParseGeneratorT:
		return
		yield

	def parseMark(self, attrs: dict[str, str] | None) -> ParseGeneratorT:
		if attrs is None or "name" not in attrs:
			return
		if self._markCallback is not None:
			name = attrs["name"]
			yield CallbackCommand(lambda: self._markCallback(name), name=f"SsmlMark_{name}")

	def convertFromXml(self, xml: str) -> SpeechSequence:
		self._cachedProsodyAttrs = []
		return super().convertFromXml(xml)
