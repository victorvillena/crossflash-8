"""Modal 8-series .syx container: split into packets, pack/unpack payload, re-seal.

Packet framing (as produced by the official updater):

    start : F0 00 21 07 <MODEL> 00 <CHIP> 7F <cntL> <cntM> F7
    data  : F0 00 21 07 <MODEL> 00 <CHIP> 00 <idxL> <idxM> <payload152> <cs nibbles> F7
    end   : F0 00 21 07 <MODEL> 00 <CHIP> 7E F7

MODEL is a single byte at offset 4 of every packet.  The payload is 19 groups of
7 bytes packed 8-for-7 (MSB bundle last); the first 128 unpacked bytes of each
packet are image bytes, the remaining 5 are padding.  The nibbles after the
payload are a non-cryptographic checksum of the unpacked payload:

    legacy (165 bytes, Argon):        cs = sum & 0xFF    -> cs[3:0], cs[7:4]
    modern (167 bytes, Cobalt/Carbon): cs = sum & 0xFFFF -> cs[11:8], cs[15:12], cs[3:0], cs[7:4]

Editing a payload byte therefore requires re-sealing that packet.
"""
from __future__ import annotations


def split_packets(raw: bytes) -> list[bytearray]:
    out: list[bytearray] = []
    i = 0
    while i < len(raw):
        if raw[i] != 0xF0:
            raise ValueError(f"expected F0 at {i}, got {raw[i]:#x}")
        j = raw.index(0xF7, i)
        out.append(bytearray(raw[i:j + 1]))
        i = j + 1
    return out


def join_packets(packets: list[bytearray]) -> bytes:
    return b"".join(bytes(p) for p in packets)


def unpack(payload: bytes) -> bytes:
    """8 payload bytes (7-bit) -> 7 firmware bytes (8-bit)."""
    out = bytearray()
    for i in range(0, len(payload) - 7, 8):
        grp = payload[i:i + 8]
        msb = grp[7]
        for j in range(7):
            out.append(grp[j] | (((msb >> (6 - j)) & 1) << 7))
    return bytes(out)


def pack(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 7):
        grp = data[i:i + 7]
        while len(grp) < 7:
            grp += b"\x00"
        msb = 0
        for j, b in enumerate(grp):
            if b & 0x80:
                msb |= 1 << (6 - j)
        out += bytes(b & 0x7F for b in grp)
        out.append(msb)
    return bytes(out)


def packet_is_data(pkt: bytearray) -> bool:
    return len(pkt) >= 11 and pkt[7] == 0x00


def reseal(pkt: bytearray) -> None:
    """Recompute and store the payload integrity value of a data packet."""
    payload = bytes(pkt[10:162])
    unp = unpack(payload)
    if len(pkt) == 167:
        cs = sum(unp) & 0xFFFF
        pkt[162] = (cs >> 8) & 0x0F
        pkt[163] = (cs >> 12) & 0x0F
        pkt[164] = cs & 0x0F
        pkt[165] = (cs >> 4) & 0x0F
    elif len(pkt) == 165:
        cs = sum(unp) & 0xFF
        pkt[162] = cs & 0x0F
        pkt[163] = (cs >> 4) & 0x0F
    else:
        raise ValueError(f"unknown data packet length {len(pkt)}")


def set_model_byte(packets: list[bytearray], model: int) -> int:
    n = 0
    for p in packets:
        if len(p) > 4 and p[4] != model:
            p[4] = model
            n += 1
    return n


# Data-packet length each model's bootloader takes, as found in that model's official files:
# Argon uses the legacy 165-byte packet (8-bit checksum), Cobalt/Carbon the 167-byte one (16-bit).
NATIVE_DATA_LEN = {0x18: 165, 0x19: 167, 0x1D: 167}


def data_len(packets: list[bytearray]) -> int | None:
    return next((len(p) for p in packets if packet_is_data(p)), None)


def reframe(packets: list[bytearray], target_len: int, target_model: int) -> list[bytearray]:
    """Rewrite every data packet as `target_len` bytes (165 legacy / 167 modern) with a fresh
    checksum, and set the model byte on every packet.  The 152-byte payload is unchanged."""
    if target_len not in (165, 167):
        raise ValueError(f"unknown data packet length {target_len}")
    out = []
    for p in packets:
        q = bytearray(p)
        if len(q) > 4:
            q[4] = target_model
        if packet_is_data(q) and len(q) != target_len:
            q = q[:162] + bytearray(target_len - 163) + b"\xF7"
            reseal(q)
        out.append(q)
    return out


def verify_seals(packets: list[bytearray]) -> int:
    bad = 0
    for p in packets:
        if not packet_is_data(p):
            continue
        payload = bytes(p[10:162])
        unp = unpack(payload)
        if len(p) == 167:
            cs = sum(unp) & 0xFFFF
            expected = ((cs >> 8) & 0x0F, (cs >> 12) & 0x0F, cs & 0x0F, (cs >> 4) & 0x0F)
            if (p[162], p[163], p[164], p[165]) != expected:
                bad += 1
        elif len(p) == 165:
            cs = sum(unp) & 0xFF
            expected = (cs & 0x0F, (cs >> 4) & 0x0F)
            if (p[162], p[163]) != expected:
                bad += 1
        else:
            bad += 1
    return bad
