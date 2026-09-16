#!/usr/bin/env python3
"""
Turn any audio file into an RTP pcap that SIPp can play as call media.

SIPp's pcap_play rewrites the IP/UDP addresses and ports at send time but keeps
the RTP payload and inter-packet timing from the capture, so this is how you get
a specific wav onto a real SIP call without any audio hardware.

The output matches the layout of SIPp's own bundled g711a.pcap: libpcap v2.4,
Ethernet link type, IPv4/UDP/RTP, one packet per frame.

Usage:
  python3 tools/wav-to-rtp-pcap.py --audio ../samples/sample-es-reservation.wav \
      --out captures/reservation-pcmu.pcap --codec pcmu

Requires ffmpeg on PATH (transcodes anything to 8 kHz mono G.711).
"""

import argparse
import os
import struct
import subprocess
import sys

CODECS = {
    "pcmu": {"payload_type": 0, "fmt": "mulaw"},
    "pcma": {"payload_type": 8, "fmt": "alaw"},
}


def transcode(audio_path, fmt):
    """Return raw 8 kHz mono G.711 bytes (one byte per sample)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", audio_path,
        "-f", fmt, "-ar", "8000", "-ac", "1",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE)
    if proc.returncode != 0 or not proc.stdout:
        sys.exit(f"ffmpeg failed to transcode {audio_path}")
    return proc.stdout


def ip_checksum(header):
    total = 0
    for i in range(0, len(header), 2):
        total += (header[i] << 8) + header[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def build_packet(payload, seq, timestamp, ssrc, payload_type):
    rtp = struct.pack("!BBHII", 0x80, payload_type, seq & 0xFFFF,
                      timestamp & 0xFFFFFFFF, ssrc) + payload

    udp_len = 8 + len(rtp)
    udp = struct.pack("!HHHH", 6000, 6000, udp_len, 0) + rtp

    total_len = 20 + udp_len
    # checksum computed over the header with the checksum field zeroed
    ip_no_sum = struct.pack("!BBHHHBBH4s4s", 0x45, 0x10, total_len, seq & 0xFFFF,
                            0, 64, 17, 0,
                            bytes((10, 1, 3, 143)), bytes((10, 1, 3, 144)))
    checksum = ip_checksum(ip_no_sum)
    ip = ip_no_sum[:10] + struct.pack("!H", checksum) + ip_no_sum[12:]

    ether = bytes.fromhex("00d05010016600047622201708 00".replace(" ", ""))
    return ether + ip + udp


def write_pcap(out_path, frames, frame_ms, payload_type):
    ssrc = 0x12345678
    samples_per_frame = 8 * frame_ms  # 8 kHz -> 8 samples per ms
    with open(out_path, "wb") as f:
        # global header: magic, v2.4, thiszone, sigfigs, snaplen, linktype=Ethernet
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for idx, frame in enumerate(frames):
            pkt = build_packet(frame, idx, idx * samples_per_frame, ssrc, payload_type)
            usec_total = idx * frame_ms * 1000
            f.write(struct.pack("<IIII", usec_total // 1_000_000,
                                usec_total % 1_000_000, len(pkt), len(pkt)))
            f.write(pkt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, help="Input audio (any ffmpeg-readable format)")
    parser.add_argument("--out", help="Output pcap path (default: <audio>.<codec>.pcap)")
    parser.add_argument("--codec", default="pcmu", choices=sorted(CODECS))
    parser.add_argument("--frame-ms", type=int, default=20, help="RTP packetization interval")
    args = parser.parse_args()

    spec = CODECS[args.codec]
    pcm = transcode(args.audio, spec["fmt"])
    frame_bytes = 8 * args.frame_ms  # G.711 is 1 byte per sample at 8 kHz
    frames = [pcm[i:i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]
    if frames and len(frames[-1]) < frame_bytes:
        frames[-1] = frames[-1] + bytes([0xFF if args.codec == "pcmu" else 0xD5]) * (
            frame_bytes - len(frames[-1]))

    out_path = args.out or f"{args.audio}.{args.codec}.pcap"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    write_pcap(out_path, frames, args.frame_ms, spec["payload_type"])

    duration = len(frames) * args.frame_ms / 1000
    print(f"wrote {out_path}: {len(frames)} RTP packets, "
          f"{args.frame_ms}ms {args.codec} (payload type {spec['payload_type']}), "
          f"{duration:.2f}s of audio")


if __name__ == "__main__":
    main()
