# A part of NonVisual Desktop Access (NVDA)
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.
# Copyright (C) 2021-2024 NV Access Limited, Joseph Lee

"""Unit tests for the Windows version module."""

import unittest
import sys
import os
import winVersion


class TestWinVersion(unittest.TestCase):
	def test_getWinVer(self):
		# Test a 3-tuple consisting of version major, minor, build.
		# sys.getwindowsversion() internally returns a named tuple, so comparing tuples is possible.
		currentWinVer = winVersion.getWinVer()
		winVerPython = sys.getwindowsversion()
		self.assertTupleEqual(
			(currentWinVer.major, currentWinVer.minor, currentWinVer.build),
			winVerPython[:3],
		)

	def test_getWinVerFromNonExistentRelease(self):
		# Test the fact that there is no Windows 10 2003 (2004 exists, however).
		with self.assertRaises(AttributeError):
			winVersion.WIN10_2003

	def test_moreRecentWinVer(self):
		# Specifically to test operators.
		minimumWinVer = winVersion.WIN81
		emojiPanelIntroduced = winVersion.WIN10_1709
		self.assertGreaterEqual(
			emojiPanelIntroduced,
			minimumWinVer,
		)

	def test_winVerKnownReleaseNameForWinVersionConstant(self):
		# Test the fact that later Windows releases provide version information in a consistent manner,
		# specifically, via Windows Registry on Windows 10 1511 and later.
		# Test with Windows Server 2016 (client release name: Windows 10 1607).
		server2016 = winVersion.WIN10_1607
		self.assertEqual(server2016.releaseName, "Windows 10 1607")

	def test_winVerKnownBuildToReleaseName(self):
		# Specifically to test if the correct release name is returned for use in getWinVer() function.
		# Try Windows 10 1809.
		knownMajor, knownMinor, knownBuild = 10, 0, 17763
		knownPublicRelease = winVersion.WinVersion(
			major=knownMajor,
			minor=knownMinor,
			build=knownBuild,
		)
		self.assertEqual(knownPublicRelease.releaseName, "Windows 10 1809")

	def test_winVerReleaseNameFromWindowsRegistry(self):
		# Test to make sure something is indeed returned from Windows Registry
		# when fetching release names for Windows 10 releases.
		# Try Windows Insider Preview build 21390, which is recorded as 'Dev".
		# But on public releases, version recorded on Windows Registry is returned.
		# This will fail if release name cannot be obtained from Windows Registry
		# ("unknown" will be recorded in release name text),
		# usually if Release Id and/or display version key is not defined.
		# For build 21390, as an Insider Preview build, "unknown" is fine
		# as this is defined for testing purposes.
		major, minor, build = 10, 0, 21390
		insiderBuild = winVersion.WinVersion(
			major=major,
			minor=minor,
			build=build,
		)
		self.assertIn(
			"unknown",
			insiderBuild.releaseName,
		)

	def test_winVerUnknownBuildToReleaseName(self):
		# It might be possible that Microsoft could use major.minor versions other than 10.0 in future releases.
		# Try Windows 8.1 which is actually version 6.3.
		unknownMajor, unknownMinor, unknownBuild = 8, 1, 0
		badWin81Info = winVersion.WinVersion(
			major=unknownMajor,
			minor=unknownMinor,
			build=unknownBuild,
		)
		self.assertEqual(badWin81Info.releaseName, "Windows release unknown")

	def test_winVerProcessorArchitecture(self):
		# See if processor architecture matches what Windows says.
		# Use os.environ to guard against platform.machine() giving odd results.
		actualArchitecture = os.environ.get("PROCESSOR_ARCHITEW6432", os.environ["PROCESSOR_ARCHITECTURE"])
		self.assertEqual(winVersion.getWinVer().processorArchitecture, actualArchitecture)

	def test_winVerUnknownWin11BuildToReleaseName(self):
		# Despite system version being 10.0, build 22000 or later is Windows 11.
		# See if build 25398 (zinc milestone) is recognized as a Windows 11 "unknown" release.
		zincMajor, zincMinor, zincBuild = 10, 0, 25398
		win11ZincInfo = winVersion.WinVersion(
			major=zincMajor,
			minor=zincMinor,
			build=zincBuild,
		)
		self.assertEqual(win11ZincInfo.releaseName, "Windows 11 unknown")
