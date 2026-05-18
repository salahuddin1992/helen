//! Mono 48 kHz cpal audio playback driven by an mpsc receiver of PCM frames.
use std::sync::{Arc, Mutex};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use ringbuf::{traits::*, HeapRb};
use tokio::sync::mpsc;
use tracing::{info, warn};

use crate::error::{CliError, Result};

const SAMPLE_RATE: u32 = 48_000;

pub struct AudioPlayback {
    _stream: cpal::Stream,
    producer: Arc<Mutex<ringbuf::HeapProd<i16>>>,
    pub tx: mpsc::Sender<Vec<i16>>,
}

impl AudioPlayback {
    pub fn start(device_name: Option<&str>) -> Result<Self> {
        let host = cpal::default_host();
        let device = match device_name {
            Some(name) => host
                .output_devices()
                .map_err(|e| CliError::Audio(e.to_string()))?
                .find(|d| d.name().map(|n| n == name).unwrap_or(false))
                .or_else(|| host.default_output_device())
                .ok_or_else(|| CliError::Audio("no output device".into()))?,
            None => host
                .default_output_device()
                .ok_or_else(|| CliError::Audio("no output device".into()))?,
        };
        let cfg = cpal::StreamConfig {
            channels: 1,
            sample_rate: cpal::SampleRate(SAMPLE_RATE),
            buffer_size: cpal::BufferSize::Default,
        };
        info!("playback device: {}", device.name().unwrap_or_else(|_| "?".into()));

        let rb = HeapRb::<i16>::new(SAMPLE_RATE as usize); // 1 s buffer
        let (prod, mut cons) = rb.split();
        let producer = Arc::new(Mutex::new(prod));

        let stream = device.build_output_stream(
            &cfg,
            move |out: &mut [f32], _| {
                for s in out.iter_mut() {
                    *s = match cons.try_pop() {
                        Some(i) => i as f32 / i16::MAX as f32,
                        None => 0.0,
                    };
                }
            },
            |e| warn!("output stream error: {e}"),
            None,
        ).map_err(|e| CliError::Audio(e.to_string()))?;
        stream.play().map_err(|e| CliError::Audio(e.to_string()))?;

        // Background pump from mpsc → ringbuf
        let (tx, mut rx) = mpsc::channel::<Vec<i16>>(64);
        let p2 = producer.clone();
        tokio::spawn(async move {
            while let Some(frame) = rx.recv().await {
                let mut g = p2.lock().unwrap();
                for s in frame {
                    let _ = g.try_push(s);
                }
            }
        });

        Ok(Self { _stream: stream, producer, tx })
    }
}
