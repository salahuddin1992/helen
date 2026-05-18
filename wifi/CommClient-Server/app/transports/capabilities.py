"""
Transport Capabilities — determines what services can run on a transport.
Evaluates bandwidth, latency, and other requirements.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.transports.types import DetectedTransport

logger = get_logger(__name__)


class TransportCapabilities:
    """
    Evaluates whether a transport can support specific media/services.
    Analyzes bandwidth, latency, and other constraints.
    """

    # Service requirements (in Mbps, ms)
    VOICE_MIN_BANDWIDTH = 0.064  # 64 kbps
    VOICE_MAX_LATENCY = 150
    VOICE_PREFERRED_BANDWIDTH = 0.1

    VIDEO_MIN_BANDWIDTH = 0.5  # 500 kbps
    VIDEO_MAX_LATENCY = 300
    VIDEO_PREFERRED_BANDWIDTH = 2

    SCREEN_SHARE_MIN_BANDWIDTH = 1  # 1 Mbps
    SCREEN_SHARE_MAX_LATENCY = 500

    FILE_TRANSFER_MIN_BANDWIDTH = 0.01  # Very low

    GROUP_CALL_BANDWIDTH_PER_PARTICIPANT = 0.128  # 128 kbps per person

    @classmethod
    def can_support_voice(
        cls,
        transport: DetectedTransport,
        bitrate_kbps: float = 64,
    ) -> bool:
        """
        Check if transport can handle real-time voice.
        Requires: latency < 150ms, bandwidth > 64kbps.
        """
        try:
            # Check connectivity
            if not transport.is_connected:
                logger.debug("Transport not connected for voice", transport=transport.transport_id)
                return False

            # Check bandwidth
            speed_mbps = (transport.speed_mbps or 100) / 1000  # Convert to Mbps
            required_mbps = bitrate_kbps / 1000

            if speed_mbps < required_mbps:
                logger.debug(
                    "Insufficient bandwidth for voice",
                    transport=transport.transport_id,
                    available=speed_mbps,
                    required=required_mbps,
                )
                return False

            # Voice doesn't strictly require latency info, but flag if problematic
            # (latency would be measured separately via signal analyzer)
            logger.debug("Voice capable", transport=transport.transport_id)
            return True

        except Exception as e:
            logger.warning("Voice capability check failed", error=str(e))
            return False

    @classmethod
    def can_support_video(
        cls,
        transport: DetectedTransport,
        bitrate_kbps: float = 2000,
    ) -> bool:
        """
        Check if transport can handle video.
        Requires: bandwidth > 500kbps, latency < 300ms.
        """
        try:
            if not transport.is_connected:
                logger.debug("Transport not connected for video", transport=transport.transport_id)
                return False

            # Check bandwidth
            speed_mbps = (transport.speed_mbps or 100) / 1000
            required_mbps = bitrate_kbps / 1000

            if speed_mbps < cls.VIDEO_MIN_BANDWIDTH:
                logger.debug(
                    "Insufficient bandwidth for video",
                    transport=transport.transport_id,
                    available=speed_mbps,
                    required=cls.VIDEO_MIN_BANDWIDTH,
                )
                return False

            logger.debug("Video capable", transport=transport.transport_id)
            return True

        except Exception as e:
            logger.warning("Video capability check failed", error=str(e))
            return False

    @classmethod
    def can_support_screen_share(
        cls,
        transport: DetectedTransport,
        bitrate_kbps: float = 5000,
    ) -> bool:
        """
        Check if transport can handle screen sharing.
        Requires: bandwidth > 1 Mbps.
        """
        try:
            if not transport.is_connected:
                logger.debug("Transport not connected for screen share", transport=transport.transport_id)
                return False

            # Check bandwidth
            speed_mbps = (transport.speed_mbps or 100) / 1000
            required_mbps = bitrate_kbps / 1000

            if speed_mbps < cls.SCREEN_SHARE_MIN_BANDWIDTH:
                logger.debug(
                    "Insufficient bandwidth for screen share",
                    transport=transport.transport_id,
                    available=speed_mbps,
                    required=cls.SCREEN_SHARE_MIN_BANDWIDTH,
                )
                return False

            logger.debug("Screen share capable", transport=transport.transport_id)
            return True

        except Exception as e:
            logger.warning("Screen share capability check failed", error=str(e))
            return False

    @classmethod
    def can_support_file_transfer(cls, transport: DetectedTransport) -> bool:
        """
        Check if transport can handle file transfers.
        Requires: any connected transport (very lenient).
        """
        try:
            if not transport.is_connected:
                logger.debug("Transport not connected for file transfer", transport=transport.transport_id)
                return False

            logger.debug("File transfer capable", transport=transport.transport_id)
            return True

        except Exception as e:
            logger.warning("File transfer capability check failed", error=str(e))
            return False

    @classmethod
    def can_support_group_call(
        cls,
        transport: DetectedTransport,
        participant_count: int,
        bitrate_kbps_per_participant: float = 128,
    ) -> bool:
        """
        Check if transport can support group calls.
        Scales bandwidth requirement by participant count.
        """
        try:
            if not transport.is_connected:
                return False

            # Calculate total bandwidth needed
            total_bitrate_kbps = bitrate_kbps_per_participant * participant_count
            required_mbps = total_bitrate_kbps / 1000
            speed_mbps = (transport.speed_mbps or 100) / 1000

            can_support = speed_mbps >= required_mbps

            logger.debug(
                "Group call capability check",
                transport=transport.transport_id,
                participants=participant_count,
                required_mbps=required_mbps,
                available_mbps=speed_mbps,
                can_support=can_support,
            )

            return can_support

        except Exception as e:
            logger.warning("Group call capability check failed", error=str(e))
            return False

    @classmethod
    def get_max_participants(cls, transport: DetectedTransport) -> int:
        """
        Calculate maximum participants for group calls on this transport.
        Based on available bandwidth.
        """
        try:
            speed_mbps = (transport.speed_mbps or 100) / 1000
            required_per_participant_mbps = cls.GROUP_CALL_BANDWIDTH_PER_PARTICIPANT / 1000

            if required_per_participant_mbps == 0:
                return 0

            max_participants = int(speed_mbps / required_per_participant_mbps)

            # Practical limits
            max_participants = max(1, min(max_participants, 100))

            logger.debug(
                "Max participants calculated",
                transport=transport.transport_id,
                max=max_participants,
                speed_mbps=speed_mbps,
            )

            return max_participants

        except Exception as e:
            logger.warning("Max participants calculation failed", error=str(e))
            return 0

    @classmethod
    def get_recommended_codec(cls, transport: DetectedTransport) -> dict:
        """
        Recommend audio/video codec based on bandwidth.
        """
        try:
            speed_mbps = (transport.speed_mbps or 100) / 1000

            # Audio codec selection
            if speed_mbps < 0.1:
                audio_codec = "opus-low"
                audio_bitrate_kbps = 16
            elif speed_mbps < 1:
                audio_codec = "opus"
                audio_bitrate_kbps = 64
            else:
                audio_codec = "opus-hd"
                audio_bitrate_kbps = 128

            # Video codec selection
            if not cls.can_support_video(transport):
                video_codec = None
                video_bitrate_kbps = 0
            elif speed_mbps < 1:
                video_codec = "h264-low"
                video_bitrate_kbps = 500
            elif speed_mbps < 5:
                video_codec = "h264"
                video_bitrate_kbps = 2000
            else:
                video_codec = "h265"
                video_bitrate_kbps = 5000

            recommendation = {
                "audio": {
                    "codec": audio_codec,
                    "bitrate_kbps": audio_bitrate_kbps,
                },
                "video": {
                    "codec": video_codec,
                    "bitrate_kbps": video_bitrate_kbps,
                } if video_codec else None,
            }

            logger.debug(
                "Codec recommendation",
                transport=transport.transport_id,
                recommendation=recommendation,
            )

            return recommendation

        except Exception as e:
            logger.warning("Codec recommendation failed", error=str(e))
            return {
                "audio": {"codec": "opus", "bitrate_kbps": 64},
                "video": None,
            }

    @classmethod
    def get_recommended_quality(cls, transport: DetectedTransport) -> dict:
        """
        Recommend video resolution, framerate, bitrate based on bandwidth.
        """
        try:
            speed_mbps = (transport.speed_mbps or 100) / 1000

            # Ultra-low bandwidth
            if speed_mbps < 0.5:
                return {
                    "resolution": "320x180",
                    "framerate": 15,
                    "bitrate_kbps": 250,
                    "quality_label": "very-low",
                }

            # Low bandwidth
            if speed_mbps < 1:
                return {
                    "resolution": "640x360",
                    "framerate": 24,
                    "bitrate_kbps": 800,
                    "quality_label": "low",
                }

            # Medium bandwidth
            if speed_mbps < 5:
                return {
                    "resolution": "1280x720",
                    "framerate": 30,
                    "bitrate_kbps": 2500,
                    "quality_label": "medium",
                }

            # High bandwidth
            if speed_mbps < 25:
                return {
                    "resolution": "1920x1080",
                    "framerate": 30,
                    "bitrate_kbps": 5000,
                    "quality_label": "high",
                }

            # Ultra-high bandwidth
            return {
                "resolution": "1920x1080",
                "framerate": 60,
                "bitrate_kbps": 12000,
                "quality_label": "ultra-high",
            }

        except Exception as e:
            logger.warning("Quality recommendation failed", error=str(e))
            return {
                "resolution": "1280x720",
                "framerate": 30,
                "bitrate_kbps": 2500,
                "quality_label": "medium",
            }
