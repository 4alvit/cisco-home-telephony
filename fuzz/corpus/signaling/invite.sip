INVITE sip:video@127.0.0.1:25004 SIP/2.0
Via: SIP/2.0/UDP 127.0.0.1:25006;branch=z9hG4bKfirst;rport
From: <sip:caller@127.0.0.1:25006>;tag=caller
To: <sip:video@127.0.0.1:25004>
Call-ID: synthetic-call
CSeq: 1 INVITE
Max-Forwards: 70
Contact: <sip:caller@127.0.0.1:25006>
Content-Type: application/sdp
Content-Length: 168

v=0
o=- 1 1 IN IP4 127.0.0.1
s=Fuzz fixture
c=IN IP4 127.0.0.1
t=0 0
m=video 25000 RTP/AVP 96
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1
a=sendonly
