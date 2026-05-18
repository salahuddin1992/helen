/// flutter_webrtc helper — peer connection lifecycle for 1:1 audio/video.
///
/// Group calls (mesh > 2 participants) and SFU routing extend this with
/// transport-specific signaling but the local-peer surface is identical.
library;

import 'dart:async';

import 'package:flutter_webrtc/flutter_webrtc.dart';

import '../core/logger/app_logger.dart';

class WebRTCService {
  WebRTCService._();
  static final WebRTCService I = WebRTCService._();

  RTCPeerConnection? _pc;
  MediaStream? _localStream;
  MediaStream? _remoteStream;

  final StreamController<MediaStream> _remoteStreamCtrl =
      StreamController<MediaStream>.broadcast();
  Stream<MediaStream> get remoteStream$ => _remoteStreamCtrl.stream;

  final StreamController<RTCIceCandidate> _localCandidates =
      StreamController<RTCIceCandidate>.broadcast();
  Stream<RTCIceCandidate> get localIceCandidates => _localCandidates.stream;

  MediaStream? get localStream => _localStream;
  RTCPeerConnection? get peer => _pc;

  Future<void> init({
    required List<Map<String, dynamic>> iceServers,
    required bool audio,
    required bool video,
  }) async {
    final Map<String, dynamic> config = <String, dynamic>{
      'iceServers': iceServers,
      'sdpSemantics': 'unified-plan',
      'bundlePolicy': 'max-bundle',
      'rtcpMuxPolicy': 'require',
    };
    _pc = await createPeerConnection(config);

    _pc!.onIceCandidate = (RTCIceCandidate c) {
      _localCandidates.add(c);
    };
    _pc!.onTrack = (RTCTrackEvent ev) {
      if (ev.streams.isNotEmpty) {
        _remoteStream = ev.streams.first;
        _remoteStreamCtrl.add(ev.streams.first);
      }
    };
    _pc!.onConnectionState = (RTCPeerConnectionState s) {
      AppLogger.I.i('rtc: connection=$s');
    };

    _localStream = await navigator.mediaDevices.getUserMedia(<String, dynamic>{
      'audio': audio,
      'video': video
          ? <String, dynamic>{
              'facingMode': 'user',
              'width': <String, int>{'ideal': 1280},
              'height': <String, int>{'ideal': 720},
              'frameRate': <String, int>{'ideal': 30},
            }
          : false,
    });
    for (final MediaStreamTrack t in _localStream!.getTracks()) {
      await _pc!.addTrack(t, _localStream!);
    }
  }

  Future<RTCSessionDescription> createOffer() async {
    final RTCSessionDescription offer = await _pc!.createOffer();
    await _pc!.setLocalDescription(offer);
    return offer;
  }

  Future<RTCSessionDescription> createAnswer(RTCSessionDescription offer) async {
    await _pc!.setRemoteDescription(offer);
    final RTCSessionDescription answer = await _pc!.createAnswer();
    await _pc!.setLocalDescription(answer);
    return answer;
  }

  Future<void> setRemoteAnswer(RTCSessionDescription answer) =>
      _pc!.setRemoteDescription(answer);

  Future<void> addRemoteCandidate(RTCIceCandidate c) =>
      _pc!.addCandidate(c);

  Future<void> toggleMute(bool mute) async {
    final MediaStream? s = _localStream;
    if (s == null) return;
    for (final MediaStreamTrack t in s.getAudioTracks()) {
      t.enabled = !mute;
    }
  }

  Future<void> toggleVideo(bool off) async {
    final MediaStream? s = _localStream;
    if (s == null) return;
    for (final MediaStreamTrack t in s.getVideoTracks()) {
      t.enabled = !off;
    }
  }

  Future<void> dispose() async {
    await _localStream?.dispose();
    await _remoteStream?.dispose();
    await _pc?.close();
    _pc = null;
    _localStream = null;
    _remoteStream = null;
  }
}
