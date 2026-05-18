//! Audio capture/playback + Opus codec for voice calls.
pub mod capture;
pub mod playback;
pub mod opus_codec;

pub use capture::AudioCapture;
pub use playback::AudioPlayback;
pub use opus_codec::OpusCodec;
