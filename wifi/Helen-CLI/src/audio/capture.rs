//! Mono 48 kHz cpal audio capture, exposing 20 ms PCM frames via channel.
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use tokio::sync::mpsc;
use tracing::{info, warn};

use crate::error::{CliError, Result};

pub const SAMPLE_RATE: u32 = 48_000;
pub const FRAME_MS: usize = 20;
pub const FRAME_SAMPLES: usize = (SAMPLE_RATE as usize / 1000) * FRAME_MS; // 960

pub struct AudioCapture {
    _stream: cpal::Stream,
    pub frames: mpsc::Receiver<Vec<i16>>,
}

impl AudioCapture {
    pub fn start(device_name: Option<&str>) -> Result<Self> {
        let host = cpal::default_host();
        let device = match device_name {
            Some(name) => host
                .input_devices()
                .map_err(|e| CliError::Audio(e.to_string()))?
                .find(|d| d.name().map(|n| n == name).unwrap_or(false))
                .or_else(|| host.default_input_device())
                .ok_or_else(|| CliError::Audio("no input device".into()))?,
            None => host
                .default_input_device()
                .ok_or_else(|| CliError::Audio("no input device".into()))?,
        };
        let cfg = cpal::StreamConfig {
            channels: 1,
            sample_rate: cpal::SampleRate(SAMPLE_RATE),
            buffer_size: cpal::BufferSize::Default,
        };
        info!("capture device: {}", device.name().unwrap_or_else(|_| "?".into()));

        let (tx, rx) = mpsc::channel::<Vec<i16>>(64);
        let mut buf: Vec<i16> = Vec::with_capacity(FRAME_SAMPLES * 2);

        let stream = device.build_input_stream(
            &cfg,
            move |data: &[f32], _| {
                for &s in data {
                    let i = (s.clamp(-1.0, 1.0) * i16::MAX as f32) as i16;
                    buf.push(i);
                    if buf.len() == FRAME_SAMPLES {
                        let frame = std::mem::take(&mut buf);
                        buf = Vec::with_capacity(FRAME_SAMPLES);
                        if tx.try_send(frame).is_err() {
                            warn!("audio capture channel full — dropping frame");
                        }
                    }
                }
            },
            |e| warn!("input stream error: {e}"),
            None,
        ).map_err(|e| CliError::Audio(e.to_string()))?;

        stream.play().map_err(|e| CliError::Audio(e.to_string()))?;
        Ok(Self { _stream: stream, frames: rx })
    }
}
