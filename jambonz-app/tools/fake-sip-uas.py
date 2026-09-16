#!/usr/bin/env python3
"""
Local stand-in for the jambonz SBC, so the injector can be validated without
burning a cluster call.

It challenges the INVITE with 401 digest (as a real SBC does), verifies the
digest response hash, checks that the ACK reuses the challenged INVITE's branch
(RFC 3261 17.1.1.3 — get this wrong and the real SBC retransmits the challenge
until sipp aborts), answers, records inbound RTP, and reports what arrived.

  python3 tools/fake-sip-uas.py
  sipp 127.0.0.1:5080 -sf tools/sipp/inject-audio.xml -i 127.0.0.1 -p 5070 \
    -min_rtp_port 6010 -max_rtp_port 6020 -s 1000 -inf users.csv \
    -key realm 127.0.0.1 -key pcap captures/reservation-pcmu.pcap \
    -d 18000 -m 1 -au testuser -ap secret
"""
import hashlib
import select
import socket
import struct
import sys
import time

SIP_PORT, RTP_PORT = 5080, 40000
REALM, NONCE, PASSWORD = "test.sip.local", "0123456789abcdef", "secret"
OUT = "/tmp/received.ulaw"


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def hdr(msg, name):
    for line in msg.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def branch_of(msg):
    via = hdr(msg, "Via")
    return via.split("branch=")[1].split(";")[0].strip() if "branch=" in via else "?"


def resp_head(msg, code, reason, extra=""):
    lines = ["SIP/2.0 %d %s" % (code, reason)]
    for name in ("Via", "From", "To", "Call-ID", "CSeq"):
        v = hdr(msg, name)
        if name == "To" and ";tag=" not in v:
            v += ";tag=uas-tag-1"
        lines.append("%s: %s" % (name, v))
    if extra:
        lines.append(extra)
    return lines


def verify_digest(auth, method):
    parts = {}
    for kv in auth.replace("Digest ", "").split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip().strip('"')
    ha1 = md5("%s:%s:%s" % (parts.get("username"), parts.get("realm"), PASSWORD))
    ha2 = md5("%s:%s" % (method, parts.get("uri")))
    ok = md5("%s:%s:%s" % (ha1, parts.get("nonce"), ha2)) == parts.get("response")
    print("    digest verified: %s (user=%s)" % (ok, parts.get("username")), flush=True)
    return ok


sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sip.bind(("127.0.0.1", SIP_PORT))
sip.setblocking(False)
rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rtp.bind(("127.0.0.1", RTP_PORT))
rtp.setblocking(False)

payloads, seqs, branches = [], [], {}
arrivals = []          # wall-clock receipt of each RTP packet, for pacing
challenged = done = False
print("fake SBC on 127.0.0.1:%d, RTP on %d" % (SIP_PORT, RTP_PORT), flush=True)

start = time.time()
while not done and time.time() - start < 90:
    for sock in select.select([sip, rtp], [], [], 0.2)[0]:
        if sock is rtp:
            try:
                pkt, _ = rtp.recvfrom(4096)
            except BlockingIOError:
                continue
            if len(pkt) > 12:
                seqs.append(struct.unpack("!H", pkt[2:4])[0])
                payloads.append(pkt[12:])
                arrivals.append(time.time())
            continue

        try:
            data, addr = sip.recvfrom(65535)
        except BlockingIOError:
            continue
        msg = data.decode(errors="replace")
        method = msg.split("\r\n")[0].split(" ")[0]
        br = branch_of(msg)
        print("<-- %s  branch=%s" % (msg.split("\r\n")[0], br), flush=True)

        if method == "INVITE":
            if not challenged:
                challenged = True
                branches["invite1"] = br
                out = resp_head(msg, 401, "Unauthorized",
                                'WWW-Authenticate: Digest realm="%s", nonce="%s", '
                                'algorithm=MD5' % (REALM, NONCE))
                out.append("Content-Length: 0")
                sip.sendto(("\r\n".join(out) + "\r\n\r\n").encode(), addr)
                print("--> 401 challenge", flush=True)
                continue
            auth = hdr(msg, "Proxy-Authorization") or hdr(msg, "Authorization")
            if auth:
                verify_digest(auth, "INVITE")
            sdp = ("v=0\r\no=uas 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\n"
                   "t=0 0\r\nm=audio %d RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
                   "a=sendrecv\r\n" % RTP_PORT)
            out = resp_head(msg, 200, "OK", "Contact: <sip:uas@127.0.0.1:%d>" % SIP_PORT)
            out += ["Content-Type: application/sdp", "Content-Length: %d" % len(sdp)]
            sip.sendto(("\r\n".join(out) + "\r\n\r\n" + sdp).encode(), addr)
            print("--> 200 OK with SDP", flush=True)

        elif method == "ACK":
            if "ack1" not in branches:
                branches["ack1"] = br
                ok = branches.get("invite1") == br
                print("    ACK branch matches challenged INVITE: %s%s" % (
                    ok, "" if ok else "   <-- real SBC would retransmit the 401"),
                    flush=True)

        elif method == "BYE":
            out = resp_head(msg, 200, "OK")
            out.append("Content-Length: 0")
            sip.sendto(("\r\n".join(out) + "\r\n\r\n").encode(), addr)
            print("--> 200 OK (BYE)", flush=True)
            done = True

with open(OUT, "wb") as f:
    for p in payloads:
        f.write(p)

print("\nRTP packets: %d (%.2fs of audio)" % (
    len(payloads), sum(len(p) for p in payloads) / 8000.0))
if seqs:
    print("seq %d..%d, gaps: %d" % (min(seqs), max(seqs),
                                    (max(seqs) - min(seqs) + 1) - len(set(seqs))))

# Pacing. Each packet carries 20ms of audio, so a sender running true to the
# clock delivers them 20ms apart and the stream lasts exactly as long as the
# audio. A slow sender stretches the stream, and a recognizer downstream sees
# longer silences than the audio really contains — which moves punctuation and
# end-of-sentence decisions. This is the control for "is the drift ours?".
if len(arrivals) > 2:
    gaps = [(arrivals[i] - arrivals[i - 1]) * 1000 for i in range(1, len(arrivals))]
    gaps_sorted = sorted(gaps)
    median = gaps_sorted[len(gaps_sorted) // 2]
    wall = arrivals[-1] - arrivals[0]
    audio = sum(len(p) for p in payloads) / 8000.0
    print("\npacing: median gap %.2fms  mean %.2fms  p95 %.2fms  max %.2fms"
          % (median, sum(gaps) / len(gaps), gaps_sorted[int(len(gaps) * 0.95)],
             gaps_sorted[-1]))
    print("        stream took %.2fs to deliver %.2fs of audio  (%+.2f%% stretch)"
          % (wall, audio, (wall / audio - 1) * 100 if audio else 0))
    print("        %s" % ("SENDER IS PACING CORRECTLY — drift seen through jambonz is "
                          "not ours" if abs(wall / audio - 1) < 0.01 else
                          "SENDER IS SLOW — the drift seen through jambonz is at "
                          "least partly our injector"))
print("branch check:", "PASS" if branches.get("invite1") == branches.get("ack1")
      else "FAIL %s" % branches)
