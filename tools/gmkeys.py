"""Read the GM Call cipher state out of a LIVE Viewer -- the measurement that
settles why our stage-2 reply is never decrypted.

WHY THIS EXISTS. Three hypotheses about the GM stage-2 keying have now been
refuted, and the fourth (that the per-slot receive context is reproducible) died
on 2026-08-16 when a `--probe-ff01` reply produced no reaction at all: the client
never read it. That left a contradiction that cannot be settled from the
decompiler, and the project's own lesson from the group-chat work applies --
reading a plausible mechanism out of a disassembly is a HYPOTHESIS, and only a
live read makes it a measurement.

THE CONTRADICTION, precisely:

  * `cft_1217_udp` RE-KEYS the per-slot context right after sending stage 1:
        uVar11 = cft_1165(0);            // a pure getter -- DAT_0386a858
        key[0] = uVar11; key[2] = 0; key[3] = 0;
        FUN_037ce080(ctx, key, key, 8);  // 8-byte re-key
    and `FUN_037ce080` writes the DERIVED key back over `key` in place, so the
    re-key's word 1 is the *derived* 0x928ac3fa, not the original key word.
  * Yet state 3 -- the only route to a differently-encrypted 80-byte datagram,
    and we demonstrably get one -- is reachable ONLY through state 2 parsing our
    0x201 successfully, which needs that same context to have decrypted it.

Both cannot be true. This tool reads the actual bytes rather than arguing.

WARNING: THE LIVE MODULE IS RELOCATED. Every address below is a VA in an image based at
polcore's preferred 0x037C0000, which is where `gmdecrypt` maps its own copy and
therefore how the decompilation numbers them. The running Viewer measured
2026-08-16 did NOT get that base, and reading the documented VAs simply failed
with nothing to say why. So each address is converted back to an RVA and re-based
on the module's actual load address, and key blob one is checked against its known
value first -- a wrong base then announces itself instead of printing plausible
garbage. polcore is also PACKED and is a COM in-proc server: it is not present at
all until you have logged in, and its .data is only correct once the POL1 stub has
unpacked it.

WHAT IT READS:

    0x0386a858  8B   DAT_0386a858 -- the re-key source word (cft_1165's return)
    0x03863140  16B  the per-slot KEY BUFFER. FUN_037ce080 rewrites this in
                     place, so after a call it holds the DERIVED key -- which is
                     what FUN_037ce600 actually schedules. If a GM call has run,
                     this is the ground truth for what the context is keyed with.
    0x038665a0  16B  key blob one, as cft_1217_udp finds it (permuted a8,ac,a0,a4
                     on the way into the key buffer)
    0x038630e0  16B  key blob two, keyed into the GLOBAL context by cft_1216
    0x0386647c  64B  the per-slot context head (schedule + CFB feedback)
    0x038665b0  64B  the global context head
    0x03863150  4B   the STATE MACHINE's current state -- the single most useful
                     number here: 3+ means our 0x201 was accepted, 1/2 means it
                     was not, and that alone resolves the contradiction.

Run ELEVATED -- the Viewer self-elevates, so an ordinary OpenProcess gets
ERROR_ACCESS_DENIED. Take one reading BEFORE pressing GM Call and one after.

Usage:  python gmkeys.py [pid]
"""
import sys, ctypes, struct
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.ReadProcessMemory.restype = wintypes.BOOL
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

#: *** POLCORE DOES NOT ALWAYS GET ITS PREFERRED BASE. *** Every address in the
#: decompilation is a VA in an image based at 0x037C0000, and `gmdecrypt` maps its
#: own copy there so those VAs work verbatim. The LIVE Viewer is a different
#: matter: measured 2026-08-16, polcore was relocated, and reading the documented
#: VAs simply failed. So every address here is turned back into an RVA and re-based
#: on the module's ACTUAL load address.
PREFERRED_BASE = 0x037C0000
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_MODULE_NAME32 = 255


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
                ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_char * (MAX_MODULE_NAME32 + 1)),
                ("szExePath", ctypes.c_char * 260)]


def module_base(pid, want="polcore"):
    """(base, size, name) for the first loaded module whose name contains `want`.

    Toolhelp rather than EnumProcessModules: it needs no extra handle rights, and
    this already runs elevated.
    """
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == wintypes.HANDLE(-1).value or snap == -1:
        return None
    me = MODULEENTRY32()
    me.dwSize = ctypes.sizeof(MODULEENTRY32)
    found, names = None, []
    if k32.Module32First(snap, ctypes.byref(me)):
        while True:
            name = me.szModule.decode("latin1", "replace")
            names.append(name)
            if want.lower() in name.lower() and found is None:
                found = (ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value,
                         me.modBaseSize, name)
            if not k32.Module32Next(snap, ctypes.byref(me)):
                break
    k32.CloseHandle(snap)
    return found if found else (None, None, names)

#: (VA, length, name, note). VAs are absolute in polcore's mapped image.
SPOTS = [
    (0x03863150, 4,  "state",       "GM state machine: >=3 means our 0x201 WAS accepted"),
    (0x0386a858, 8,  "DAT_0386a858", "cft_1165(0) return -- the re-key source word"),
    (0x03863140, 16, "keybuf",      "per-slot key buffer (DERIVED in place by ce080)"),
    (0x038665a0, 16, "keyblob1",    "key blob one, pre-permutation"),
    (0x038630e0, 16, "keyblob2",    "key blob two -> the global context"),
    (0x03863130, 8,  "sessdwords",  "DAT_03863130/34 -- what state 7 echo-checks"),
    (0x03866464, 8,  "endpoint",    "{u16 port @+2, u32 ip @+4} the redirect target"),
    (0x038662b0, 4,  "retry",       "retransmit counter -- 5 is the give-up point"),
    # *** THE ONE THAT SETTLES THE KEY QUESTION. *** FUN_037ce1e0 decrypts IN
    # PLACE into this buffer, so after state 2 has handled a datagram this holds
    # OUR OWN REPLY as the client actually saw it. `MAG?` at +0 means the key is
    # right and the rejection is further along (checksum, or the +0x0A/+0x0C echo);
    # garbage means the key is still wrong. No inference required either way.
    (0x038639bc, 0x40, "msgbuf",    "the client's message buffer -- OUR REPLY, DECRYPTED"),
    # 0xB0, not 0x40: FUN_037cfd70 writes the trailing block back to
    # ctx+0x9c and a remainder count to ctx+0xa4. Those are the CFB feedback state,
    # and a fresh context of ours only matches if they are at their initial values.
    (0x0386647c, 0xB0, "ctx_slot",  "per-slot cipher context (+0x9c feedback, +0xa4 remainder)"),
    (0x038665b0, 0xB0, "ctx_global", "global cipher context"),
]

#: What we expect key blob one to be, so a wrong base announces itself instead of
#: printing plausible garbage. Verified by gmdecrypt.
EXPECT_BLOB1 = bytes.fromhex("8ad4a0a055b3feb0a22452bc26b9612b")


k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.Module32First.restype = wintypes.BOOL
k32.Module32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32)]
k32.Module32Next.restype = wintypes.BOOL
k32.Module32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32)]
k32.CloseHandle.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]


def dbgpriv():
    try:
        adv = ctypes.WinDLL("advapi32")
        class LUID(ctypes.Structure):
            _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.LONG)]
        class LAA(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]
        class TP(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("priv", LAA)]
        tok = wintypes.HANDLE()
        if adv.OpenProcessToken(k32.GetCurrentProcess(), 0x28, ctypes.byref(tok)):
            luid = LUID()
            if adv.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
                adv.AdjustTokenPrivileges(tok, False, ctypes.byref(TP(1, LAA(luid, 2))),
                                          0, None, None)
    except Exception:
        pass


def find_pid():
    import subprocess
    out = subprocess.check_output(["tasklist", "/fo", "csv", "/nh"], text=True)
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if parts and parts[0].lower() == "pol.exe":
            return int(parts[1])
    return None


def read(h, va, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(va), buf, size, ctypes.byref(got))
    return buf.raw[:got.value] if ok and got.value else None


def main():
    dbgpriv()
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else find_pid()
    if not pid:
        print("pol.exe is not running -- start the Viewer and log in first")
        return 1
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print(f"OpenProcess failed ({ctypes.get_last_error()}) -- run this ELEVATED; "
              "the Viewer self-elevates and an ordinary handle is refused")
        return 1
    print(f"pol.exe pid {pid}")

    mod = module_base(pid, "polcore")
    if not mod or mod[0] is None:
        print("polcore.dll is not loaded in that process. Log in first -- it is a "
              "COM in-proc server and does not load at startup.")
        if mod and mod[2]:
            print("modules seen: " + ", ".join(sorted(mod[2]))[:400])
        return 1
    base, size, name = mod
    slide = base - PREFERRED_BASE
    print(f"{name} @ {base:#010x} ({size} bytes), slide {slide:+#x} "
          f"from the documented base {PREFERRED_BASE:#010x}\n")

    def at(va):
        """Documented VA -> live VA."""
        return va + slide

    blob1 = read(h, at(0x038665a0), 16)
    if blob1 is None:
        print("could not read key blob one even after rebasing -- the page is not "
              "committed. polcore is packed; if you have only just logged in, the "
              "POL1 stub may not have unpacked .data yet.")
        return 1
    if blob1 != EXPECT_BLOB1:
        print("!! key blob one does not match its known value, so these addresses\n"
              "   are NOT landing where they should. Everything below is suspect --\n"
              "   do not read conclusions out of it.\n"
              f"   read   {blob1.hex()}\n   expect {EXPECT_BLOB1.hex()}\n")
    else:
        print("key blob one matches -- the rebase is correct, the rest is trustworthy\n")

    for va, size, name, note in SPOTS:
        data = read(h, at(va), size)
        if data is None:
            print(f"{name:12} @{va:#010x}  <unreadable>")
            continue
        if size == 4:
            print(f"{name:12} @{va:#010x}  {struct.unpack('<I', data)[0]:#010x}"
                  f"  ({struct.unpack('<i', data)[0]})   {note}")
        elif size == 8:
            a, b = struct.unpack("<II", data)
            print(f"{name:12} @{va:#010x}  {a:#010x} {b:#010x}   {note}")
        else:
            print(f"{name:12} @{va:#010x}  {note}")
            for off in range(0, size, 16):
                print(f"             +{off:02x}  {data[off:off+16].hex(' ')}")
        if name == "msgbuf":
            # Self-interpreting, because this is the line the whole question turns
            # on and hex is easy to read hopefully rather than carefully.
            if data[:4] == b"MAG?":
                mtype = struct.unpack_from("<H", data, 6)[0]
                mlen = struct.unpack_from("<H", data, 8)[0]
                ea = struct.unpack_from("<H", data, 0x0A)[0]
                ec = struct.unpack_from("<I", data, 0x0C)[0]
                print(f"             ^^ *** 'MAG?' -- OUR REPLY DECRYPTED CLEANLY. ***")
                print(f"                type={mtype:#06x} len={mlen:#x} "
                      f"echo +0x0A={ea:#06x} +0x0C={ec:#010x}")
                # THE STATE-7 GATE, decoded side by side. -0x2a3 (POL-0675) has
                # exactly one source in polcore: body +0x10/+0x14 not matching
                # DAT_03863130/34. Printing both together turns "it errored" into
                # "here is the pair that disagreed".
                if len(data) >= 0x30:
                    got_a = struct.unpack_from("<I", data, 0x28)[0]   # body +0x10
                    got_b = struct.unpack_from("<I", data, 0x2C)[0]   # body +0x14
                    want = read(h, at(0x03863130), 8)
                    wa, wb = struct.unpack("<II", want) if want else (None, None)
                    ok = (got_a == wa and got_b == wb)
                    print(f"                state-7 echo: we sent {got_a:#010x} / "
                          f"{got_b:#010x}")
                    print(f"                              it wants {wa:#010x} / "
                          f"{wb:#010x}   -> {'MATCH' if ok else '*** MISMATCH = POL-0675 ***'}")
                    if not ok:
                        print("                The reply is otherwise perfect, so this"
                              " pair is the ONLY thing left.")
            elif data == bytes(len(data)):
                print("             ^^ empty -- no datagram has been decrypted into "
                      "this buffer at all.\n                Our reply is not reaching "
                      "the client, or not reaching THIS slot.")
            else:
                print("             ^^ not 'MAG?' -- the decrypt produced garbage, so "
                      "the KEY IS STILL WRONG.\n                Compare against what "
                      "gmserver last sent; the first 8 bytes are one CFB block.")
        if name == "keybuf":
            # The whole point: if this still equals the permuted blob-one key the
            # re-key has NOT run; if it holds the derived words it has.
            permuted = blob1[8:16] + blob1[0:8]        # a8, ac, a0, a4
            derived1 = bytes.fromhex("838256cdfac38a92d9b7bdc2d189a0c0")
            if data == permuted:
                print("             ^^ RAW permuted key -- ce080 has NOT run yet")
            elif data == derived1:
                print("             ^^ the 16-byte DERIVED key -- ce080 ran ONCE, "
                      "the 8-byte re-key has NOT")
            elif data == bytes(16):
                # The headline, not a footnote: this is the key a reply has to be
                # encrypted under, and it is why KEY16-encrypted replies were
                # silently dropped.
                print("             ^^ *** ALL ZERO *** -- the 8-byte re-key ran "
                      "with DAT_0386a858=0, and ce080 maps all-zero in to all-zero\n"
                      "                out. ANY REPLY MUST BE ENCRYPTED UNDER A ZERO "
                      "KEY (gmserver does this by default).")
            else:
                print("             ^^ neither the raw nor the once-derived key, so "
                      "the 8-byte RE-KEY HAS RUN. Word 0 of the input was "
                      "DAT_0386a858 above.")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
