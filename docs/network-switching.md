# Change Wi-Fi after the demo works

**Draft procedure; not yet validated on the trial hardware.** The board reports
NetworkManager 1.46.0; checkpoint syntax was checked against that version's source.
Record hardware results separately;
do not treat these instructions as evidence that a network switch succeeded.

**Trial checkpoint, 2026-10-06:** wired setup, fresh login and filesystem expansion
passed. The demo installer built both engines, enabled its services and passed
readiness. The first image check failed; a verified HWC/CHW export mismatch was
then corrected and the vision engine rebuilt. **The repeated text and three
known-image checks now pass the bounded signs-of-life gate**, through the API and
browser UI. Minor unsupported details remain; this is not a general accuracy or
performance benchmark. See the [recorded results](first-boot-recovery.md#what-has-been-observed).
Ethernet remains active, Wi-Fi is disconnected, and temporary build swap is off.
Wi-Fi has not been tested or a target network selected. Reconnect and authenticate
the identified USB console before any handover. Installation success does not
waive the prerequisites below.

Start only after wired setup, a fresh login, adequate SD space, and **a real image
request producing a coherent caption matching the known image content** have passed. A ready endpoint alone is not
enough. Keep Ethernet connected and an authenticated USB serial console open on
the identified board. Use the console for changes so losing an SSH route does not
lose control. Leave other Orins untouched.

The final state for this procedure is **Ethernet active and preferred, with the
tested Wi-Fi profile saved but disconnected**. Network names, addresses, UUIDs and
test output belong in private deployment notes, not source control.

## 1. Identify interfaces and profiles

Run on the verified Orin, through its USB console:

```bash
hostname
whoami
findmnt -n -o SOURCE /
nmcli --version
systemctl is-active NetworkManager
nmcli -f DEVICE,TYPE,STATE,CONNECTION device status
nmcli -f NAME,UUID,TYPE,DEVICE connection show
ip -brief address
ip -4 route show default
ip -6 route show default
```

Confirm the board using the previously recorded USB/SSH identity, not its hostname
alone. Select the actual wired Ethernet and Wi-Fi devices; exclude USB networking,
bridges and VPNs. If NetworkManager does not manage them, stop and inspect the
installed networking configuration instead of starting a second network manager.

Replace every placeholder before using the remaining commands:

```bash
ETH_IFACE='<observed-wired-interface>'
ETH_UUID='<active-wired-profile-uuid>'
WIFI_IFACE='<observed-wifi-interface>'
TEST_HTTPS_URL='<reachable-public-HTTPS-URL>'
```

Record the selected profiles' current autoconnect and IPv4/IPv6 route settings
locally before changing them. Profile UUIDs avoid ambiguities from duplicate names.
Never use `--show-secrets`, `wifi show-password`, or copy NetworkManager keyfiles
into reports. [NetworkManager profile selection and activation](https://networkmanager.dev/docs/api/latest/nmcli.html#connection).

## 2. Join Wi-Fi without unplugging Ethernet

For a human, run `sudo nmtui`, choose **Activate a connection**, and select the
intended wireless network. Enter credentials directly in the terminal, without
displaying them or recording that interaction. Keep the wired connection active.
Then continue with the interface checks below.
[NetworkManager's text interface](https://networkmanager.dev/docs/api/latest/nmtui.html).

For a command-line session, inspect available networks on the selected device:

```bash
nmcli radio wifi
nmcli device wifi list ifname "$WIFI_IFACE"
```

If Wi-Fi is disabled, establish why before enabling it. Use an existing matching
profile by its observed UUID, if available:

```bash
WIFI_UUID='<selected-existing-wifi-profile-uuid>'
sudo nmcli --ask connection up uuid "$WIFI_UUID" ifname "$WIFI_IFACE"
```

Otherwise, for an ordinary DHCP network supported by the simple Wi-Fi connection
command, let the human enter the password at the prompt:

```bash
sudo nmcli --ask device wifi connect '<chosen-SSID>' ifname "$WIFI_IFACE"
nmcli -f NAME,UUID,TYPE,DEVICE connection show --active
WIFI_UUID='<uuid-of-the-activated-wifi-profile>'
```

Do not put a password in command arguments, environment variables, history, chat
or scripts. An agent can prepare the command, then leave the secret prompt to the
human in Terminal. For enterprise/802.1X networking, use the required approved
profile and certificates; the simple connection command does not configure those.
[Official connection examples](https://networkmanager.dev/docs/api/latest/nmcli-examples.html).

## 3. Test the Wi-Fi path explicitly

Keep Ethernet connected during this first test:

```bash
nmcli -f GENERAL,IP4,IP6 device show "$WIFI_IFACE"
ip -4 route show dev "$WIFI_IFACE"
ip -6 route show dev "$WIFI_IFACE"
curl --interface "$WIFI_IFACE" --connect-timeout 10 --max-time 20 \
  --fail --silent --show-error --output /dev/null "$TEST_HTTPS_URL"
```

Confirm that Wi-Fi has a usable address and routes. The curl request binds its
connection to that interface; an ordinary successful request could still use
Ethernet. Name resolution can still use the system resolver, so this is not yet
proof of a complete Wi-Fi-only setup. Do not add insecure TLS flags to make an
unexplained failure pass. If interface binding needs privileges on the installed
system, repeat that specific check with `sudo` after inspecting the error.
[curl interface selection](https://curl.se/docs/manpage.html#--interface).

From the client, open the UI at the newly observed Wi-Fi address and verify it is
the same board. Check its known SSH fingerprint before accepting SSH at a new
address. Submit a real image and read its caption. **With Ethernet still active,
this establishes address reachability, not a Wi-Fi-only inference path.** On the
same subnet, Linux can accept or return traffic through Ethernet even when the
destination is the Wi-Fi address. The interface-bound curl check above and the
handover below establish different checkpoints; keep their results separate.
[Linux interface/ARP behavior](https://docs.kernel.org/networking/ip-sysctl.html#arp-filter-boolean).
Keep browser/network timing separate from server inference timing.

## 4. Validate a brief Wi-Fi-only handover

Use the USB console, with no active installer/download. Check capability directly:

```bash
nmcli device checkpoint --help
```

NetworkManager 1.46.0 supports the two interface arguments below even though its
checkpoint help omits them, and its top-level device help may omit `checkpoint`
entirely. The version's parser consumes the supplied interface names before `--`,
limiting the snapshot to these two devices. Do not drop those names and silently
checkpoint every interface. [NetworkManager 1.46.0 parser](https://github.com/NetworkManager/NetworkManager/blob/1.46.0/src/nmcli/devices.c#L4818).

After Wi-Fi has passed the bound-interface check, run:

```bash
sudo nmcli device checkpoint --timeout 90 "$ETH_IFACE" "$WIFI_IFACE" -- \
  nmcli device disconnect "$ETH_IFACE"
```

Read the child command's output: merely reaching the confirmation prompt does
not prove Ethernet disconnected. The 90-second rollback window starts when the
checkpoint is created, not when the confirmation prompt appears. While it waits,
test a fresh UI request and one real inference at the observed Wi-Fi address.
Confirm only after the disconnect and intended path are verified, using the exact
word requested by the prompt (`Yes` in English, not `y`). Otherwise leave it
unconfirmed. Timeout attempts restoration and normally returns exit status 3;
verify the actual Ethernet state and routes afterward instead of inferring either
rollback success or failure from that status alone.

After confirmation, inspect addresses and routes and repeat the bound-interface
HTTPS check. An Ethernet device marked disconnected by NetworkManager is not, by
itself, proof of the path taken by a same-subnet browser request. For a strict
Wi-Fi-only inference result, also verify that the request/response uses Wi-Fi with
no Ethernet traffic, or test with the Ethernet cable physically unplugged while
retaining USB control. In the latter case, replug it before expecting automatic
wired restoration. Exclude alternative VPN/USB upstream routes. If the path cannot
be established, record only the narrower connectivity result.
[NetworkManager 1.46.0 confirmation handling](https://github.com/NetworkManager/NetworkManager/blob/1.46.0/src/nmcli/devices.c#L4709),
[rollback implementation](https://github.com/NetworkManager/NetworkManager/blob/1.46.0/src/core/nm-checkpoint.c#L188).

Do not perform this handover from the only available SSH session. If checkpoint
support or the USB console is unavailable, keep Ethernet active and report the
handover as untested. Inspect one failure before trying again; do not cycle
profiles or reset the networking stack blindly.

## 5. Restore Ethernet as the final connection

Finish the checkpoint first: either commit it or wait for its removal and inspect
the resulting state. Do not change final profile settings while a live checkpoint
could still restore older values. Use the USB console to reactivate the exact saved
wired profile:

```bash
sudo nmcli connection up uuid "$ETH_UUID" ifname "$ETH_IFACE"
nmcli -f GENERAL,IP4,IP6 device show "$ETH_IFACE"
curl --interface "$ETH_IFACE" --connect-timeout 10 --max-time 20 \
  --fail --silent --show-error --output /dev/null "$TEST_HTTPS_URL"
```

After Ethernet passes, keep the selected Wi-Fi profile saved for manual use and
disconnect that Wi-Fi device:

```bash
sudo nmcli connection modify uuid "$WIFI_UUID" connection.autoconnect no
sudo nmcli device disconnect "$WIFI_IFACE"
nmcli -f NAME,UUID,TYPE,DEVICE connection show
nmcli -f DEVICE,TYPE,STATE,CONNECTION device status
ip -4 route show default
ip -6 route show default
```

Confirm Ethernet owns the applicable default routes, Wi-Fi is disconnected, and
its profile still exists. Setting this profile's `connection.autoconnect` to `no`
is intentional: future use requires explicit activation. Other saved profiles or
VPNs need separate review; do not change them silently or claim reboot persistence
without testing it. Autoconnect priority chooses profiles, while route metrics
govern route preference; changing one does not replace checking the other.
[Connection autoconnect settings](https://networkmanager.dev/docs/api/latest/settings-connection.html),
[IPv4 route metrics](https://networkmanager.dev/docs/api/latest/settings-ipv4.html).

Reopen the UI at the verified Ethernet address and check another real caption.
To use the saved Wi-Fi later, explicitly activate its UUID with the `--ask`
command above, then repeat the connectivity checks before changing Ethernet.

## Agent completion record

Record each checkpoint as **observed**, **failed**, or **not tested**:

- Correct board, authenticated console, and real wired inference baseline.
- Selected Wi-Fi profile/device, address and routes; no secrets captured.
- Interface-bound connectivity and real caption over the Wi-Fi address.
- Optional Wi-Fi-only handover, rollback behavior, and any routing limitations.
- Ethernet restored, final real caption, Wi-Fi profile saved and disconnected.

This draft supplies a procedure only. Hardware results are pending; a completed
command or an active NetworkManager profile alone does not establish all checks.
