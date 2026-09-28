"""Auth node constants: the token-0 key trick, the session cipher IV, NoPad reply lines."""
import sessioncrypt



# --------------------------------------------------------------------------- #
# Auth node RESPONDER (token=0 -> forces client K=0)
# --------------------------------------------------------------------------- #
# The client derives the Blowfish key from the token in our server's 300 line:
#   K = byte_reverse(low8( base64decode(token) ^e mod n ))   (sub_001386c0)
# base64decode of an all-'T' token (A64[0]) is all zeros, so base=0, modexp=0,
# and K = 8 zero bytes -- a known key, independent of SE's RSA (e,n). We send
# that token so the NEXT hop's NICK is encrypted under K=0, which both proves the
# trick and lets us recover the IV from the known 'NICK UH5GRSV86 ' plaintext.
A64 = "TSG8IncW3HFKokOg79qzeCmZs2yBYEQVAUxR5rbwi4P@jMDLtpvad0f_J1hlN6uX"
TOKEN0 = A64[0] * 47                        # decodes to all-zero -> base 0 -> K=0
K0_IV = bytes.fromhex("4f5f4d4661d9c59f")   # = SE modulus[0:8], recovered
_P0, _S0 = sessioncrypt.bf_setkey(b"\x00" * 8)


class NoPad(bytes):
    """A reply line that must be framed WITHOUT frame_line()'s pad byte.

    The PC and PS2 sides disagree by one byte here and both readings are right
    for their own client. `frame_line()` checksums realtext+pad and puts the pad
    on the wire, which reproduces a captured PC NICK exactly; the PS2 boot ELF's
    emitter at 0x00135294 covers `len - 6` bytes, i.e. text + 4 checksum chars +
    CRLF and no pad at all. The checksum verifies either way (the pad is simply
    inside the covered span), so this is not about validation -- it is that a
    trailing space becomes part of the TEXT the game parses, and a game line is
    a fixed 44 characters. IRC numerics do not care; a 'B' record might.
    """
