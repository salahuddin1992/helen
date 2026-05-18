import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:go_router/go_router.dart';

import '../../../services/webrtc_service.dart';

class ActiveCallScreen extends ConsumerStatefulWidget {
  const ActiveCallScreen({super.key, required this.callId});
  final String callId;

  @override
  ConsumerState<ActiveCallScreen> createState() => _ActiveCallScreenState();
}

class _ActiveCallScreenState extends ConsumerState<ActiveCallScreen> {
  final RTCVideoRenderer _local = RTCVideoRenderer();
  final RTCVideoRenderer _remote = RTCVideoRenderer();
  bool _muted = false;
  bool _videoOff = false;

  @override
  void initState() {
    super.initState();
    _initRenderers();
  }

  Future<void> _initRenderers() async {
    await _local.initialize();
    await _remote.initialize();
    final WebRTCService rtc = WebRTCService.I;
    if (rtc.localStream != null) _local.srcObject = rtc.localStream;
    rtc.remoteStream$.listen((MediaStream s) {
      _remote.srcObject = s;
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _local.dispose();
    _remote.dispose();
    super.dispose();
  }

  Future<void> _hangup() async {
    await WebRTCService.I.dispose();
    if (mounted) context.pop();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: Stack(
          children: <Widget>[
            Positioned.fill(child: RTCVideoView(_remote)),
            Positioned(
              right: 16,
              top: 16,
              width: 110,
              height: 160,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: RTCVideoView(_local, mirror: true),
              ),
            ),
            Positioned(
              left: 0,
              right: 0,
              bottom: 24,
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: <Widget>[
                  _btn(
                    icon: _muted ? Icons.mic_off : Icons.mic,
                    color: _muted ? Colors.red : Colors.white,
                    onTap: () async {
                      setState(() => _muted = !_muted);
                      await WebRTCService.I.toggleMute(_muted);
                    },
                  ),
                  _btn(
                    icon: Icons.call_end,
                    color: Colors.white,
                    bg: Colors.red,
                    onTap: _hangup,
                  ),
                  _btn(
                    icon: _videoOff ? Icons.videocam_off : Icons.videocam,
                    color: _videoOff ? Colors.red : Colors.white,
                    onTap: () async {
                      setState(() => _videoOff = !_videoOff);
                      await WebRTCService.I.toggleVideo(_videoOff);
                    },
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _btn({
    required IconData icon,
    required Color color,
    Color bg = const Color(0x33FFFFFF),
    required VoidCallback onTap,
  }) {
    return InkResponse(
      onTap: onTap,
      child: Container(
        width: 64,
        height: 64,
        decoration: BoxDecoration(color: bg, shape: BoxShape.circle),
        child: Icon(icon, color: color, size: 28),
      ),
    );
  }
}
