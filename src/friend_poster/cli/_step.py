# UTF-8
"""One-off probes. Not the GUI."""

from __future__ import annotations

import os
import sys
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    step = sys.argv[1] if len(sys.argv) > 1 else "xxtouch"
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        if step == "xxtouch":
            # Do not send unquoted === to zsh: =cmd is equals-expansion and becomes "== not found".
            code, out = phone.exec_ok(
                "echo '-- dpkg --'; dpkg -l 2>/dev/null | grep -i xxtouch | head -10; "
                "echo '-- home --'; ls -ld /var/mobile/Media/1ferver /var/mobile/Media/1ferver/lua/scripts 2>&1; "
                "echo '-- bin --'; ls -l /var/jb/usr/local/xxtouch/bin /usr/local/xxtouch/bin 2>&1 | head -30; "
                "echo '-- which --'; command -v croissant; command -v lua; "
                "echo '-- listen --'; netstat -an 2>/dev/null | grep 46952 | head -5; "
                "echo '-- apps --'; ls -1 /var/jb/Applications 2>/dev/null | grep -i xx; "
                "echo '-- ps --'; ps aux 2>/dev/null | grep -i xxtouch | grep -v grep | head -10",
                timeout=25,
            )
            print("EXIT", code)
            print(out)
            print("STEP2_XXTOUCH_PROBE=DONE")
            return 0
        if step == "xxtouch-launch":
            code, out = phone.exec_ok(
                "echo '-- croissant --'; head -40 /var/jb/usr/local/xxtouch/bin/croissant; "
                "echo '-- uiopen --'; command -v uiopen; "
                "echo '-- Info --'; "
                "ls -1 /var/jb/Applications/XXTExplorer.app/Info.plist "
                "/var/jb/Applications/XXTouch.app/Info.plist 2>&1",
                timeout=15,
            )
            print("EXIT", code)
            print(out)
            for bid in (
                "ch.xxtou.XXTouchElite",
                "ch.xxtou.xxtouchelite",
                "ch.xxtou.XXTExplorer",
                "ch.xxtou.xxtouch",
            ):
                c, o = phone.exec_ok(f"uiopen --bundleid {bid}", timeout=10)
                print(f"uiopen {bid} exit={c} {o.strip()[:200]}")
            import time

            time.sleep(4.0)
            code, out = phone.exec_ok(
                "echo '-- listen --'; netstat -an 2>/dev/null | grep 46952 | head -8; "
                "echo '-- ps --'; ps aux 2>/dev/null | grep -Ei 'xxtouch|ngservice|croissant|1ferver' | grep -v grep | head -20",
                timeout=15,
            )
            print(out)
            print("STEP3_XXTOUCH_LAUNCH=DONE")
            return 0
        if step == "xxtouch-id":
            code, out = phone.exec_ok(
                "echo '-- bundle --'; "
                "plutil -p /var/jb/Applications/XXTExplorer.app/Info.plist 2>/dev/null | "
                "grep -E 'CFBundle(Identifier|DisplayName|Executable)|DTPlatformVersion'; "
                "echo '-- apps dir --'; ls -la /var/jb/Applications 2>&1 | head -40; "
                "echo '-- daemons --'; ls -1 /var/jb/Library/LaunchDaemons /Library/LaunchDaemons "
                "/var/jb/Library/LaunchAgents /var/mobile/Library/LaunchAgents 2>/dev/null | grep -Ei 'xx|ngservice|1ferver'; "
                "echo '-- 1ferver --'; ls -la /var/mobile/Media/1ferver 2>&1; "
                "echo '-- activate --'; cat /var/jb/usr/local/xxtouch/bin/activate; "
                "echo '-- plist daemons --'; ls -1 /var/jb/Library/LaunchDaemons 2>/dev/null | head -40",
                timeout=20,
            )
            print("EXIT", code)
            print(out)
            print("STEP4_XXTOUCH_ID=DONE")
            return 0
        if step == "xxtouch-lua":
            ident, _ = phone.exec_ok(
                "echo '-- conf --'; cat /var/mobile/Media/1ferver/1ferver.conf; echo; "
                "echo '-- etc --'; ls -la /var/jb/usr/local/xxtouch/etc 2>&1; "
                "echo '-- log --'; ls -ld /var/mobile/Media/1ferver/log /var/jb/usr/local/xxtouch/log 2>&1; "
                "echo '-- bundle strings --'; "
                "strings /var/jb/Applications/XXTExplorer.app/Info.plist 2>/dev/null | grep -E 'ch\\.xxtou|CFBundle' | head -20",
                timeout=15,
            )
            print(ident)
            lua_src = (
                'print("LUA_OK")\n'
                'print("screen", type(screen))\n'
                'print("touch", type(touch))\n'
                'local ok, a, b = pcall(function()\n'
                '  if screen and screen.size then return screen.size() end\n'
                '  return "no-size"\n'
                'end)\n'
                'print("size", ok, a, b)\n'
            )
            phone.sftp_write("/var/mobile/Media/1ferver/lua/scripts/ra_probe.lua", lua_src)
            code, out = phone.exec_ok(
                ". /var/jb/usr/local/xxtouch/bin/activate; "
                "lua /var/mobile/Media/1ferver/lua/scripts/ra_probe.lua",
                timeout=20,
            )
            print("LUA_EXIT", code)
            print(out)
            print("STEP5_XXTOUCH_LUA=DONE")
            return 0
        if step == "xxtouch-log":
            code, out = phone.exec_ok(
                "echo '-- log ls --'; ls -la /var/mobile/Media/1ferver/log /var/jb/usr/local/xxtouch/log 2>&1; "
                "echo '-- write --'; "
                "touch /var/mobile/Media/1ferver/log/ra_write_test 2>&1; echo touch_exit:$?; "
                "echo '-- conf --'; cat /var/mobile/Media/1ferver/1ferver.conf; echo; "
                "echo '-- bundle --'; "
                "strings /var/jb/Applications/XXTExplorer.app/Info.plist 2>/dev/null | grep -E 'ch\\.xxtou|CFBundle' | head -20",
                timeout=15,
            )
            print("EXIT", code)
            print(out)
            print("STEP6_XXTOUCH_LOG=DONE")
            return 0
        if step == "xxtouch-api":
            from friend_poster.core.config import XXT_LOCAL_PORT, XXT_REMOTE_PORT, SSH_LOCAL_HOST
            import urllib.error
            import urllib.request

            code, out = phone.exec_ok(
                "echo '-- listen --'; netstat -an 2>/dev/null | grep 46952 | head -10; "
                "echo '-- ps --'; ps aux 2>/dev/null | grep -Ei 'xxtouch|ngservice|XXTExplorer' | grep -v grep | head -15",
                timeout=15,
            )
            print("EXIT", code)
            print(out)
            try:
                phone._forward(XXT_LOCAL_PORT, XXT_REMOTE_PORT)
            except PhoneError as e:
                print("FORWARD_FAIL", e)
                print("STEP7_XXTOUCH_API=FAIL")
                return 1
            url = f"http://{SSH_LOCAL_HOST}:{XXT_LOCAL_PORT}/is_running"
            req = urllib.request.Request(url, data=b"", method="POST")
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    body = r.read().decode("utf-8", errors="replace")
                print("API_STATUS", r.status)
                print("API_BODY", body[:500])
                print("STEP7_XXTOUCH_API=OK")
                return 0
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                print("API_HTTP", e.code, body[:400])
                print("STEP7_XXTOUCH_API=HTTP")
                return 0
            except Exception as e:
                print("API_FAIL", type(e).__name__, e)
                print("STEP7_XXTOUCH_API=FAIL")
                return 1
        if step == "xxtouch-dump":
            from friend_poster.core.config import XXT_LOCAL_PORT, XXT_REMOTE_PORT, SSH_LOCAL_HOST, XXT_SCRIPTS
            import json
            import time
            import urllib.request

            lua_src = (
                'screen.init(0)\n'
                'local w, h = screen.size()\n'
                'local path = "/var/mobile/Media/1ferver/lua/scripts/ra_size.txt"\n'
                'local fh = io.open(path, "w")\n'
                'fh:write(tostring(w) .. "x" .. tostring(h))\n'
                'fh:close()\n'
            )
            phone.sftp_write(f"{XXT_SCRIPTS}/ra_size.lua", lua_src)
            phone.exec_ok("rm -f /var/mobile/Media/1ferver/lua/scripts/ra_size.txt", timeout=8)
            try:
                phone._forward(XXT_LOCAL_PORT, XXT_REMOTE_PORT)
            except PhoneError as e:
                print("FORWARD_FAIL", e)
                return 1

            def post(path: str, payload: bytes) -> str:
                req = urllib.request.Request(
                    f"http://{SSH_LOCAL_HOST}:{XXT_LOCAL_PORT}{path}",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.read().decode("utf-8", errors="replace")

            print("SELECT", post("/select_script_file", json.dumps({"filename": "ra_size.lua"}).encode("utf-8")))
            print("LAUNCH", post("/launch_script_file", b""))
            time.sleep(3.0)
            print("IS_RUNNING", post("/is_running", b""))
            try:
                blob = phone.sftp_read(f"{XXT_SCRIPTS}/ra_size.txt")
            except Exception as e:
                print("READ_FAIL", e)
                print("STEP8_XXTOUCH_DUMP=FAIL")
                return 1
            print("SIZE_FILE", blob.strip())
            print("STEP8_XXTOUCH_DUMP=OK" if "x" in blob else "STEP8_XXTOUCH_DUMP=EMPTY")
            return 0
        if step == "hme-dump":
            import time

            from friend_poster.core.config import CREATE_LABELS, HME_SETTINGS_URL
            from friend_poster.device.ui_tree import TapStack, dump_named, find_label

            stack = TapStack(phone, log)
            backend = stack.attach()
            print("BACKEND", backend)
            stack.open_url(HME_SETTINGS_URL)
            time.sleep(3.0)
            blob, nodes = dump_named(stack, phone, "03_hme_before_create", log)
            create = find_label(nodes, CREATE_LABELS)
            print("CREATE", None if create is None else f"{create.labels()!r} center={create.center()}")
            print("STEP9_HME_DUMP=OK")
            print("NO_TAP")
            return 0
        if step == "save-icloud":
            from friend_poster.device.ssh_device import update_secrets

            pwd = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
            if not pwd:
                print("ERROR missing password argv")
                return 2
            update_secrets(icloud_password=pwd)
            print("SAVED_ICLOUD_PASSWORD=yes")
            return 0
        if step == "hme-now":
            import time

            from friend_poster.core.config import CREATE_LABELS
            from friend_poster.device.hidemyemail import _unlock_icloud_if_needed
            from friend_poster.device.ui_tree import TapStack, dump_named, find_label, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "03_hme_now", log)
            nodes = _unlock_icloud_if_needed(stack, phone, log, nodes, None)
            create = find_label(nodes, CREATE_LABELS)
            hme = find_label_exact(nodes, ("Hide My Email",))
            print("CREATE", None if create is None else f"{create.labels()!r} center={create.center()}")
            print("HME_ROW", None if hme is None else f"{hme.labels()!r} center={hme.center()}")
            print("STEP9_HME_NOW=OK")
            print("NO_TAP")
            return 0
        if step == "hme-create":
            from friend_poster.device.hidemyemail import create_one_hide_my_email
            from friend_poster.device.ui_tree import TapStack

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            addr = create_one_hide_my_email(phone, stack, log, open_settings=False)
            print("HME_ADDR", addr)
            print("STEP10_HME_CREATE=OK")
            return 0
        if step == "hme-label":
            from friend_poster.device.hidemyemail import _forbidden
            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "05_hme_label", log)
            nxt = find_label_exact(nodes, ("Next", "Done", "Save"))
            print("NEXT", None if nxt is None else f"{nxt.labels()!r} center={nxt.center()}")
            if nxt is None or _forbidden(nxt.labels()) or not stack.can_tap():
                print("STEP11_HME_LABEL=NO_TAP")
                return 1
            log(f"Tapping label-screen confirm from dump: {nxt.labels()!r} {nxt.center()}")
            stack.tap_xy(*nxt.center())
            import time

            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "06_hme_after_label", log)
            print("STEP11_HME_LABEL=OK")
            return 0
        if step == "hme-label-ui":
            import time

            from friend_poster.core.config import XXT_SCRIPTS
            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            lua = """
local path = "/var/mobile/Media/1ferver/lua/scripts/ra_label.txt"
local fh = io.open(path, "w")
local okreq, ui_element = pcall(require, "ui_element")
if not okreq then
  fh:write("NO_MODULE " .. tostring(ui_element) .. "\\n")
  fh:close()
  return
end
local item, err = ui_element.find({ title = "Next" })
if not item then
  item, err = ui_element.find({ title = "Done" })
end
if item then
  local ok, info = ui_element.click(item)
  fh:write("CLICK " .. tostring(ok) .. " " .. tostring(info) .. "\\n")
else
  fh:write("FIND_FAIL " .. tostring(err) .. "\\n")
end
fh:close()
"""
            phone.exec_ok("rm -f /var/mobile/Media/1ferver/lua/scripts/ra_label.txt", timeout=8)
            try:
                stack._run_xxt_lua("ra_label.lua", lua, timeout=25, wait_path=f"{XXT_SCRIPTS}/ra_label.txt")
            except PhoneError as e:
                print("LUA_FAIL", e)
            else:
                print("UI", phone.sftp_read(f"{XXT_SCRIPTS}/ra_label.txt").strip())
            time.sleep(1.5)
            blob, nodes = dump_named(stack, phone, "07_hme_after_label_ui", log)
            still = find_label_exact(nodes, ("Label your address",))
            print("STILL_LABEL", still is not None)
            print("STEP12_HME_LABEL_UI=DONE")
            return 0
        if step == "hme-back":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "08_hme_label_back", log)
            back = find_label_exact(nodes, ("Back",))
            nxt = find_label_exact(nodes, ("Next", "Done"))
            print("BACK", None if back is None else f"{back.labels()!r} center={back.center()}")
            print("NEXT", None if nxt is None else f"{nxt.labels()!r} center={nxt.center()}")
            if back is None or not stack.can_tap():
                print("STEP13_HME_BACK=NO_TAP")
                return 1
            log(f"Tapping Back from dump: {back.labels()!r} {back.center()}")
            stack.tap_xy(*back.center())
            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "09_hme_after_back", log)
            still = find_label_exact(nodes, ("Label your address",))
            print("STILL_LABEL", still is not None)
            print("STEP13_HME_BACK=OK")
            return 0
        if step == "hme-finish":
            import time

            from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "10_hme_finish_before", log)
            cont = find_label_exact(nodes, ("Continue",))
            if cont and stack.can_tap():
                log(f"Tapping Continue from dump: {cont.labels()!r} {cont.center()}")
                stack.tap_xy(*cont.center())
                time.sleep(2.5)
                blob, nodes = dump_named(stack, phone, "11_hme_finish_label", log)
            fields = [
                n
                for n in nodes
                if collapsed_text(n.labels()) == "label your address" and n.on_screen()
            ]
            field = max(fields, key=lambda n: n.y) if fields else None
            print("LABEL_FIELD", None if field is None else f"{field.labels()!r} center={field.center()} y={field.y}")
            if field is None or not stack.can_tap():
                print("STEP14_HME_FINISH=NO_FIELD")
                return 1
            log(f"Tapping label field from dump: {field.center()}")
            stack.tap_xy(*field.center())
            time.sleep(0.8)
            stack.type_text("mail")
            time.sleep(1.2)
            blob, nodes = dump_named(stack, phone, "12_hme_finish_typed", log)
            nxt = find_label_exact(nodes, ("Next", "Done"))
            print("NEXT", None if nxt is None else f"{nxt.labels()!r} center={nxt.center()}")
            if nxt is None or not stack.can_tap():
                print("STEP14_HME_FINISH=NO_NEXT")
                return 1
            log(f"Tapping Next from dump: {nxt.labels()!r} {nxt.center()}")
            stack.tap_xy(*nxt.center())
            time.sleep(2.5)
            blob, nodes = dump_named(stack, phone, "13_hme_finish_after", log)
            still = find_label_exact(nodes, ("Label your address",))
            print("STILL_LABEL", still is not None)
            print("STEP14_HME_FINISH=OK")
            return 0
        if step == "hme-done":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "14_hme_allset", log)
            done = find_label_exact(nodes, ("Done",))
            print("DONE", None if done is None else f"{done.labels()!r} center={done.center()}")
            if done is None or not stack.can_tap():
                print("STEP15_HME_DONE=NO_TAP")
                return 1
            log(f"Tapping Done from dump: {done.labels()!r} {done.center()}")
            stack.tap_xy(*done.center())
            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "15_hme_after_done", log)
            print("STEP15_HME_DONE=OK")
            return 0
        if step == "tweak-inv":
            code, out = phone.exec_ok(
                "echo '-- dpkg --'; dpkg -l 2>/dev/null | grep -Ei 'crane|shadow|ghost|choicy|sandy|ellekit|substrate' | head -40; "
                "echo '-- apps --'; ls -1 /var/jb/Applications 2>&1; "
                "echo '-- dylibs --'; ls -1 /var/jb/Library/MobileSubstrate/DynamicLibraries 2>&1; "
                "echo '-- tweakinject --'; ls -1 /var/jb/usr/lib/TweakInject 2>&1 | head -40; "
                "echo '-- prefs jb --'; ls -l /var/jb/var/mobile/Library/Preferences 2>&1 | grep -Ei 'crane|shadow|ghost|choicy|sgwc|jjolano|opa334' ; "
                "echo '-- prefs mobile --'; ls -l /var/mobile/Library/Preferences 2>&1 | grep -Ei 'crane|shadow|ghost|choicy|sgwc|jjolano|opa334' ; "
                "echo '-- prefloader --'; ls -1 /var/jb/Library/PreferenceLoader/Preferences 2>&1 | grep -Ei 'ghost|shadow|crane|choicy'; "
                "echo '-- crane support --'; ls -ld '/var/mobile/Library/Application Support/Crane' "
                "'/var/jb/var/mobile/Library/Application Support/Crane' 2>&1; "
                "echo '-- daemons --'; ls -1 /var/jb/Library/LaunchDaemons 2>/dev/null | grep -Ei 'crane|shadow|ghost|choicy'; "
                "echo '-- user apps --'; ls -1 /var/containers/Bundle/Application 2>/dev/null | head -5; "
                "for d in /var/containers/Bundle/Application/*; do "
                "n=`ls -1 \"$d\" 2>/dev/null | grep '\\.app$' | head -1`; "
                "[ -n \"$n\" ] && echo APP \"$n\"; "
                "done 2>/dev/null | head -80",
                timeout=30,
            )
            print("EXIT", code)
            print(out)
            print("STEP_TWEAK_INV=DONE")
            return 0
        if step == "tweak-prefs":
            code, out = phone.exec_ok(
                "echo '-- craneprefs --'; "
                "plutil -p /var/jb/var/mobile/Library/Preferences/com.opa334.craneprefs.plist 2>&1; "
                "echo '-- crane dylib filter --'; cat /var/jb/Library/MobileSubstrate/DynamicLibraries/Crane.plist 2>&1; "
                "echo '-- ghost filter --'; cat /var/jb/Library/MobileSubstrate/DynamicLibraries/Ghost.plist 2>&1; "
                "echo '-- shadow filter --'; cat /var/jb/Library/MobileSubstrate/DynamicLibraries/Shadow.plist 2>&1; "
                "echo '-- shadow settings --'; cat /var/jb/Library/PreferenceLoader/Preferences/ShadowSettings.plist 2>&1 | head -80; "
                "echo '-- ghost prefloader --'; ls -la /var/jb/Library/PreferenceLoader/Preferences /var/jb/Library/PreferenceBundles /var/jb/Library/PreferenceBundles/Ghost* /var/jb/Applications/Ghost.app 2>&1 | head -60; "
                "echo '-- ghost roots --'; ls -1 /var/jb/Applications/Ghost.app 2>&1 | head -40; "
                "echo '-- reddit info --'; "
                "for d in /var/containers/Bundle/Application/*; do "
                "if ls \"$d\" 2>/dev/null | grep -q RedditApp.app; then "
                "echo DIR=$d; ls \"$d/RedditApp.app\" | head -5; "
                "plutil -p \"$d/RedditApp.app/Info.plist\" 2>/dev/null | grep -E 'CFBundleIdentifier|CFBundleDisplayName|MinimumOSVersion|CFBundleShortVersionString'; "
                "fi; done; "
                "echo '-- choicy --'; dpkg -l 2>/dev/null | grep -i choicy; ls /var/jb/Library/MobileSubstrate/DynamicLibraries/*hoicy* 2>&1",
                timeout=25,
            )
            from friend_poster.core.config import DUMP_DIR

            os.makedirs(DUMP_DIR, exist_ok=True)
            path = os.path.join(DUMP_DIR, "tweak-prefs.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(out)
            print("EXIT", code)
            print("WROTE", path, "chars", len(out))
            print("STEP_TWEAK_PREFS=DONE")
            return 0
        if step == "tweak-plists":
            import plistlib

            from friend_poster.core.config import DUMP_DIR

            os.makedirs(DUMP_DIR, exist_ok=True)
            pulls = (
                "/var/jb/var/mobile/Library/Preferences/com.opa334.craneprefs.plist",
                "/var/jb/Library/MobileSubstrate/DynamicLibraries/ Crane.plist",
                "/var/jb/Library/MobileSubstrate/DynamicLibraries/Ghost.plist",
                "/var/jb/Library/MobileSubstrate/DynamicLibraries/Shadow.plist",
                "/var/jb/Applications/CraneApplication.app/Info.plist",
                "/var/jb/Applications/Ghost.app/Info.plist",
            )
            for remote in pulls:
                local_name = remote.replace(" ", "_").replace("/", "_").strip("_") + ".txt"
                local = os.path.join(DUMP_DIR, local_name)
                try:
                    data = phone.sftp_read_bytes(remote)
                except Exception as e:
                    print("MISS", remote, e)
                    continue
                try:
                    parsed = plistlib.loads(data)
                    text = repr(parsed)
                except Exception:
                    text = repr(data[:400])
                with open(local, "w", encoding="utf-8") as f:
                    f.write(text + "\n")
                print("OK", remote, "->", local_name, "len", len(text))
            print("STEP_TWEAK_PLISTS=DONE")
            return 0
        if step == "crane-open":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            for bid in (
                "com.opa334.crane",
                "com.opa334.CraneApplication",
                "com.opa334.Crane",
            ):
                c, o = phone.exec_ok(f"uiopen --bundleid {bid}", timeout=10)
                print("uiopen", bid, c, (o or "").strip()[:120])
            time.sleep(3.0)
            dump_named(stack, phone, "20_crane_open", log)
            print("STEP_CRANE_OPEN=OK")
            return 0
        if step == "crane-settings":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            for url in (
                "prefs:root=Crane",
                "App-prefs:root=Crane",
                "prefs:root=com.opa334.crane",
            ):
                c, o = phone.exec_ok("uiopen '" + url.replace("'", "") + "'", timeout=10)
                print("uiopen", url, c, (o or "").strip()[:80])
            time.sleep(2.5)
            dump_named(stack, phone, "21_crane_settings", log)
            print("STEP_CRANE_SETTINGS=OK")
            return 0
        if step == "crane-apps":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            phone.exec_ok("uiopen 'prefs:root=Crane'", timeout=10)
            time.sleep(2.5)
            blob, nodes = dump_named(stack, phone, "22_crane_before_apps", log)
            apps = find_label_exact(nodes, ("Applications",))
            print("APPLICATIONS", None if apps is None else f"{apps.labels()!r} {apps.center()}")
            if apps is None or not stack.can_tap():
                print("STEP_CRANE_APPS=NO_TAP")
                return 1
            log(f"Tapping Applications from dump: {apps.center()}")
            stack.tap_xy(*apps.center())
            time.sleep(2.5)
            dump_named(stack, phone, "23_crane_applications", log)
            print("STEP_CRANE_APPS=OK")
            return 0
        if step == "crane-calc":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "24_crane_apps_now", log)
            calc = find_label_exact(nodes, ("Calculator",))
            print("CALC", None if calc is None else f"{calc.labels()!r} {calc.center()}")
            if calc is None or not stack.can_tap():
                print("STEP_CRANE_CALC=NO_TAP")
                return 1
            log(f"Tapping Calculator from dump: {calc.center()}")
            stack.tap_xy(*calc.center())
            time.sleep(2.5)
            dump_named(stack, phone, "25_crane_calculator", log)
            print("STEP_CRANE_CALC=OK")
            return 0
        if step == "crane-add":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "26_crane_calc_now", log)
            add = find_label_exact(nodes, ("Add",))
            print("ADD", None if add is None else f"{add.labels()!r} {add.center()}")
            if add is None or not stack.can_tap():
                print("STEP_CRANE_ADD=NO_TAP")
                return 1
            log(f"Tapping Add from dump: {add.center()}")
            stack.tap_xy(*add.center())
            time.sleep(2.5)
            dump_named(stack, phone, "27_crane_after_add", log)
            print("STEP_CRANE_ADD=OK")
            return 0
        if step == "crane-name":
            import time

            from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "28_crane_name_sheet", log)
            name = find_label_exact(nodes, ("Name",))
            adds = [
                n
                for n in nodes
                if collapsed_text(n.labels()) == "add" and n.on_screen()
            ]
            dialog_add = max(adds, key=lambda n: n.x) if adds else None
            print("NAME", None if name is None else name.center())
            print("DIALOG_ADD", None if dialog_add is None else f"{dialog_add.labels()!r} {dialog_add.center()}")
            if name and stack.can_tap():
                stack.tap_xy(*name.center())
                time.sleep(0.5)
            stack.type_text("test1")
            time.sleep(0.8)
            if dialog_add is None or not stack.can_tap():
                print("STEP_CRANE_NAME=NO_ADD")
                return 1
            log(f"Tapping dialog Add from dump: {dialog_add.center()}")
            stack.tap_xy(*dialog_add.center())
            time.sleep(2.5)
            dump_named(stack, phone, "29_crane_named", log)
            print("STEP_CRANE_NAME=OK")
            return 0
        if step == "crane-active":
            import time

            from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "30_crane_before_active", log)
            active = find_label_exact(nodes, ("Active Container",))
            defaults = [
                n
                for n in nodes
                if collapsed_text(n.labels()) == "default" and n.on_screen()
            ]
            picker = None
            if active and defaults:
                picker = min(
                    defaults,
                    key=lambda n: abs(n.y - active.y) + (0 if n.x > active.x else 400),
                )
            print("ACTIVE", None if active is None else active.center())
            print("PICKER_DEFAULT", None if picker is None else picker.center())
            if picker is None or not stack.can_tap():
                print("STEP_CRANE_ACTIVE=NO_PICKER")
                return 1
            log(f"Tapping Active Container value from dump: {picker.center()}")
            stack.tap_xy(*picker.center())
            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "31_crane_active_picker", log)
            test1 = find_label_exact(nodes, ("test1",))
            print("TEST1", None if test1 is None else f"{test1.labels()!r} {test1.center()}")
            if test1 is None or not stack.can_tap():
                print("STEP_CRANE_ACTIVE=NO_TEST1")
                return 1
            log(f"Tapping test1 from dump: {test1.center()}")
            stack.tap_xy(*test1.center())
            time.sleep(2.0)
            dump_named(stack, phone, "32_crane_active_set", log)
            print("STEP_CRANE_ACTIVE=OK")
            return 0
        if step == "crane-active2":
            import time

            from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "33_crane_active_again", log)
            on_picker = any(
                collapsed_text(n.labels()) == "default" for n in nodes if n.on_screen()
            ) and not any(
                collapsed_text(n.labels()) == "containers" for n in nodes if n.on_screen()
            )
            test1 = find_label_exact(nodes, ("test1",))
            print("ON_PICKER", on_picker, "TEST1", None if test1 is None else test1.center())
            if on_picker and test1 and stack.can_tap():
                # OCR boxes are 24x24 at the left of the row; tap further into the cell.
                x = min(test1.x + 180, 360)
                y = test1.center()[1]
                log(f"Tapping test1 row from dump: ({x}, {y})")
                stack.tap_xy(x, y)
                time.sleep(2.5)
                blob, nodes = dump_named(stack, phone, "34_crane_after_test1", log)
            has_containers = any(
                collapsed_text(n.labels()) == "containers" for n in nodes if n.on_screen()
            )
            for n in nodes:
                if collapsed_text(n.labels()) in ("test1", "default") and n.on_screen():
                    print("ROW", collapsed_text(n.labels()), n.center())
            print("HAS_CONTAINERS", has_containers)
            print("STEP_CRANE_ACTIVE2=OK")
            return 0
        if step == "crane-back":
            import time

            from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "35_crane_before_back", log)
            nav = [
                n
                for n in nodes
                if n.on_screen() and n.y < 110 and "calculator" in collapsed_text(n.labels())
            ]
            print("NAV", [(collapsed_text(n.labels()), n.center()) for n in nav])
            if not stack.can_tap():
                print("STEP_CRANE_BACK=NO_TAP")
                return 1
            # Same Settings nav as dump 30: back control was Applications at (140, 75).
            log("Tapping Settings nav back slot from dump 30 Applications (140, 75)")
            stack.tap_xy(140, 75)
            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "36_crane_after_back", log)
            for n in nodes:
                blobt = collapsed_text(n.labels())
                if blobt in ("test1", "default", "active container", "containers"):
                    print("ROW", blobt, n.center())
            print("STEP_CRANE_BACK=OK")
            return 0
        if step == "shadow-open":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            for url in (
                "prefs:root=Shadow",
                "App-prefs:root=Shadow",
                "prefs:root=me.jjolano.shadow",
            ):
                c, o = phone.exec_ok("uiopen '" + url.replace("'", "") + "'", timeout=10)
                print("uiopen", url, c, (o or "").strip()[:80])
            time.sleep(2.5)
            dump_named(stack, phone, "40_shadow_settings", log)
            print("STEP_SHADOW_OPEN=OK")
            return 0
        if step == "shadow-apps":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "41_shadow_now", log)
            apps = find_label_exact(nodes, ("Applications",))
            print("APPLICATIONS", None if apps is None else f"{apps.labels()!r} {apps.center()}")
            if apps is None or not stack.can_tap():
                print("STEP_SHADOW_APPS=NO_TAP")
                return 1
            log(f"Tapping Shadow Applications from dump: {apps.center()}")
            stack.tap_xy(*apps.center())
            time.sleep(2.5)
            dump_named(stack, phone, "42_shadow_applications", log)
            print("STEP_SHADOW_APPS=OK")
            return 0
        if step == "shadow-reddit":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            phone.exec_ok("uiopen 'prefs:root=Shadow'", timeout=10)
            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "43_shadow_home", log)
            apps = find_label_exact(nodes, ("Applications",))
            if apps and stack.can_tap():
                log(f"Tapping Applications from dump: {apps.center()}")
                stack.tap_xy(*apps.center())
                time.sleep(2.0)
                blob, nodes = dump_named(stack, phone, "44_shadow_app_list", log)
            reddit = find_label_exact(nodes, ("Reddit",))
            print("REDDIT", None if reddit is None else f"{reddit.labels()!r} {reddit.center()}")
            if reddit is None or not stack.can_tap():
                print("STEP_SHADOW_REDDIT=NO_TAP")
                return 1
            log(f"Tapping Reddit from dump: {reddit.center()}")
            stack.tap_xy(*reddit.center())
            time.sleep(2.5)
            dump_named(stack, phone, "45_shadow_reddit", log)
            print("STEP_SHADOW_REDDIT=OK")
            return 0
        if step == "shadow-enable":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "46_shadow_reddit_now", log)
            enable = find_label_exact(nodes, ("Enable Shadow",))
            print("ENABLE", None if enable is None else f"{enable.labels()!r} {enable.center()}")
            if enable is None or not stack.can_tap():
                print("STEP_SHADOW_ENABLE=NO_TAP")
                return 1
            log(f"Tapping Enable Shadow from dump: {enable.center()}")
            stack.tap_xy(*enable.center())
            time.sleep(1.5)
            dump_named(stack, phone, "47_shadow_enabled", log)
            print("STEP_SHADOW_ENABLE=OK")
            return 0
        if step == "ghost-open":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            for bid in (
                "com.sgwc.Ghost",
                "com.sgwc.ghost",
            ):
                c, o = phone.exec_ok(f"uiopen --bundleid {bid}", timeout=10)
                print("uiopen", bid, c, (o or "").strip()[:120])
            time.sleep(3.0)
            dump_named(stack, phone, "50_ghost_open", log)
            print("STEP_GHOST_OPEN=OK")
            return 0
        if step == "ghost-apps":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "51_ghost_now", log)
            local = find_label_exact(nodes, ("Local",))
            apps = find_label_exact(nodes, ("Application List",))
            print("LOCAL", None if local is None else local.center())
            print("APPLIST", None if apps is None else apps.center())
            if local and stack.can_tap():
                log(f"Tapping Ghost Local from dump: {local.center()}")
                stack.tap_xy(*local.center())
                time.sleep(1.2)
                blob, nodes = dump_named(stack, phone, "52_ghost_local", log)
                apps = find_label_exact(nodes, ("Application List",))
            if apps is None or not stack.can_tap():
                print("STEP_GHOST_APPS=NO_LIST")
                return 1
            log(f"Tapping Application List from dump: {apps.center()}")
            stack.tap_xy(*apps.center())
            time.sleep(2.5)
            dump_named(stack, phone, "53_ghost_app_list", log)
            print("STEP_GHOST_APPS=OK")
            return 0
        if step == "ghost-reddit":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "54_ghost_list_now", log)
            user = find_label_exact(nodes, ("User Apps",))
            search = find_label_exact(nodes, ("Search apps...", "Q Search apps..."))
            print("USER", None if user is None else user.center())
            print("SEARCH", None if search is None else search.center())
            if user and stack.can_tap():
                log(f"Tapping User Apps from dump: {user.center()}")
                stack.tap_xy(*user.center())
                time.sleep(1.5)
                blob, nodes = dump_named(stack, phone, "55_ghost_user_apps", log)
            reddit = find_label_exact(nodes, ("Reddit",))
            print("REDDIT", None if reddit is None else f"{reddit.labels()!r} {reddit.center()}")
            if reddit is None and search is None:
                search = find_label_exact(nodes, ("Search apps...", "Q Search apps..."))
            if reddit is None and search and stack.can_tap():
                log(f"Tapping Ghost search from dump: {search.center()}")
                stack.tap_xy(*search.center())
                time.sleep(0.6)
                stack.type_text("Reddit")
                time.sleep(1.5)
                blob, nodes = dump_named(stack, phone, "56_ghost_search", log)
                reddit = find_label_exact(nodes, ("Reddit",))
            if reddit is None or not stack.can_tap():
                print("STEP_GHOST_REDDIT=NO_REDDIT")
                return 1
            log(f"Tapping Ghost Reddit from dump: {reddit.center()}")
            stack.tap_xy(*reddit.center())
            time.sleep(2.5)
            dump_named(stack, phone, "57_ghost_reddit", log)
            print("STEP_GHOST_REDDIT=OK")
            return 0
        if step == "ghost-enable":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            blob, nodes = dump_named(stack, phone, "58_ghost_reddit_now", log)
            enable = find_label_exact(nodes, ("Enable", "Enable Ghost for Reddit"))
            print("ENABLE", None if enable is None else f"{enable.labels()!r} {enable.center()}")
            if enable is None or not stack.can_tap():
                print("STEP_GHOST_ENABLE=NO_TAP")
                return 1
            log(f"Tapping Enable from dump: {enable.center()}")
            stack.tap_xy(*enable.center())
            time.sleep(1.5)
            dump_named(stack, phone, "59_ghost_enabled", log)
            print("STEP_GHOST_ENABLE=OK")
            return 0
        if step == "dismiss":
            import time

            from friend_poster.device.ui_tree import TapStack, dump_named, find_label_exact

            stack = TapStack(phone, log)
            print("BACKEND", stack.attach())
            try:
                stack._xxt_post("/recycle", b"", timeout=5)
            except Exception as e:
                log(f"recycle skipped: {e}")
            blob, nodes = dump_named(stack, phone, "69_dismiss_before", log)
            btn = find_label_exact(nodes, ("Dismiss", "OK", "Close"))
            print("BTN", None if btn is None else f"{btn.labels()!r} {btn.center()}")
            if btn is None or not stack.can_tap():
                print("STEP_DISMISS=NO_ALERT")
                return 0
            log(f"Tapping Dismiss from dump: {btn.center()}")
            stack.tap_xy(*btn.center())
            time.sleep(1.2)
            dump_named(stack, phone, "69_dismiss_after", log)
            print("STEP_DISMISS=OK")
            return 0
        print("unknown step", step)
        return 2
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
