# Verified facts about the Cisco batch

Reviewed October 16, 2026. Gmail confirmed a batch of **10 × Cisco 7945G** phones
with handsets and stands, delivered December 1, 2022. This identifies the order's
model, not the phones' current condition, MACs, hardware revisions or firmware.
Check each label before a batch rollout.

- **The 7945G has a color display, two line keys, Gigabit Ethernet, SCCP and SIP
  support.** This model does not support a KEM. See the
  [Cisco phone comparison](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cvd/G2srmdbrnt/G2srmdbrnt_Book/FeatServ.html).
- **PoE is built in: IEEE 802.3af, Class 3.** No hardware modification is needed.
  See the [Cisco PoE FAQ](https://www.cisco.com/c/en/us/support/docs/voice-unified-communications/unified-ip-phone-7900-series/97869-poe-requirement-faq.html)
  and [power wiring/budget guide](poe.md).
- The firmware family is **`7945_7965`**, with separate SIP and SCCP distributions.
  This is Enterprise SIP, not the licensed MPP conversion used for 7800/8800 phones.
- **9.4(2)SR3 is not the latest release found.** Cisco's September 2025 device pack
  lists `7945_7965-sip.9-4-2SR4-3`, version `9.4(2SR4.3)`. This identifies a package
  to obtain; it does not establish that flashing has been completed.
  See [Cisco Device Package 14.0(1.15072-1)](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cucm/device_packs/1401-devpack-for-sep-2025.html).
- Cisco supports a SIP firmware ZIP for manual TFTP deployment without CUCM.
  Keep signed components together; a `.loads` file alone is insufficient.
  See the [Cisco 7945/7965 SIP README](https://www.cisco.com/web/software/282074288/128566/cmterm-7945_7965-sip.9-4-2SR2-2-readme.html).
- **The upgrade path depends on hardware and App Load.** A starting version below
  8.3(3) requires an intermediate version. For hardware 13.0+, the 9.3(1)SR4 notes
  specify a minimum of 9.3(1)SR3. Do not install 8.5(2) on every phone first.
  See [Cisco Hardware Updates / Installation](https://www.cisco.com/en/US/docs/voice_ip_comm/cuipph/7900_series/firmware/9_3_1SR4/english/release_notes/P790_BK_RF3660D6_00_rn-9_3_1sr4-7900-series_chapter_00.pdf).
- 7945G support ended on **June 30, 2023**. Use a dedicated voice network and
  local services. See the [Cisco 7900 support page](https://www.cisco.com/c/en/us/support/collaboration-endpoints/unified-ip-phone-7900-series/series.html).

At the initial research date, individual App Load/VID values, an authorized
firmware archive and home hardware parameters had not been verified. Subsequent
server deployment and the first phone's discovery are recorded separately in
[live validation](server-validation.md). Real phone calls remain untested;
generated configuration alone is not proof of physical deployment.

Follow the [full conversion procedure](sip-conversion.md). Cisco documentation
establishes hardware capabilities, but does not guarantee this Asterisk
integration. XML also follows the
[USECALLMANAGER author's primary documentation](https://usecallmanager.nz/sepmac-cnf-xml.html).
Start with a single-phone pilot.
