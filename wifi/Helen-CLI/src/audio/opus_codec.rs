//! Opus encode/decode wrappers — 48 kHz mono voice.
use opus::{Application, Channels, Decoder as OpusDec, Encoder as OpusEnc};

use crate::audio::capture::FRAME_SAMPLES;
use crate::error::{CliError, Result};

const SAMPLE_RATE: u32 = 48_000;
const MAX_PACKET: usize = 4000;

pub struct OpusCodec {
    enc: OpusEnc,
    dec: OpusDec,
    packet: Vec<u8>,
    pcm: Vec<i16>,
}

impl OpusCodec {
    pub fn new() -> Result<Self> {
        let enc = OpusEnc::new(SAMPLE_RATE, Channels::Mono, Application::Voip)
            .map_err(|e| CliError::Audio(format!("opus enc: {e}")))?;
        let dec = OpusDec::new(SAMPLE_RATE, Channels::Mono)
            .map_err(|e| CliError::Audio(format!("opus dec: {e}")))?;
        Ok(Self {
            enc, dec,
            packet: vec![0u8; MAX_PACKET],
            pcm: vec![0i16; FRAME_SAMPLES],
        })
    }

    /// Encode one 20 ms PCM frame into an Opus packet.
    pub fn encode(&mut self, pcm: &[i16]) -> Result<Vec<u8>> {
        if pcm.len() != FRAME_SAMPLES {
            return Err(CliError::Audio(format!(
                "frame size mismatch: got {}, expected {}",
                pcm.len(), FRAME_SAMPLES
            )));
        }
        let n = self.enc.encode(pcm, &mut self.packet)
            .map_err(|e| CliError::Audio(format!("opus encode: {e}")))?;
        Ok(self.packet[..n].to_vec())
    }

    pub fn decode(&mut self, packet: &[u8]) -> Result<Vec<i16>> {
        let n = self.dec.decode(packet, &mut self.pcm, false)
            .map_err(|e| CliError::Audio(format!("opus decode: {e}")))?;
        Ok(self.pcm[..n].to_vec())
    }
}
