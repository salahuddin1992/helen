//! On-demand screen capture via Windows GDI BitBlt with optional consent
//! dialog and PNG encoding.

use crate::config::AgentConfig;
use crate::error::{AgentError, Result};

#[cfg(target_os = "windows")]
pub async fn capture_with_consent(cfg: &AgentConfig) -> Result<Vec<u8>> {
    if cfg.screen_capture_requires_consent && !show_consent_dialog().await? {
        return Err(AgentError::CommandRejected("screen capture denied by user".into()));
    }
    tokio::task::spawn_blocking(capture_primary)
        .await
        .map_err(|e| AgentError::Internal(e.to_string()))?
}

#[cfg(not(target_os = "windows"))]
pub async fn capture_with_consent(_cfg: &AgentConfig) -> Result<Vec<u8>> {
    Err(AgentError::CommandRejected("screen capture only supported on Windows".into()))
}

#[cfg(target_os = "windows")]
fn capture_primary() -> Result<Vec<u8>> {
    use windows::Win32::Foundation::HWND;
    use windows::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC,
        GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS,
        HGDIOBJ, SRCCOPY,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN,
    };

    unsafe {
        let width = GetSystemMetrics(SM_CXSCREEN);
        let height = GetSystemMetrics(SM_CYSCREEN);
        if width <= 0 || height <= 0 {
            return Err(AgentError::Internal("invalid screen dimensions".into()));
        }

        let screen_dc = GetDC(HWND(std::ptr::null_mut()));
        if screen_dc.0.is_null() {
            return Err(AgentError::Internal("GetDC failed".into()));
        }
        let mem_dc = CreateCompatibleDC(screen_dc);
        let bitmap = CreateCompatibleBitmap(screen_dc, width, height);
        let old = SelectObject(mem_dc, HGDIOBJ(bitmap.0));

        if BitBlt(mem_dc, 0, 0, width, height, screen_dc, 0, 0, SRCCOPY).is_err() {
            SelectObject(mem_dc, old);
            let _ = DeleteObject(HGDIOBJ(bitmap.0));
            let _ = DeleteDC(mem_dc);
            ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);
            return Err(AgentError::Internal("BitBlt failed".into()));
        }

        let mut bmi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: width,
                biHeight: -height, // top-down
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0 as u32,
                biSizeImage: 0,
                biXPelsPerMeter: 0,
                biYPelsPerMeter: 0,
                biClrUsed: 0,
                biClrImportant: 0,
            },
            bmiColors: Default::default(),
        };

        let pixel_count = (width * height) as usize;
        let mut buf: Vec<u8> = vec![0; pixel_count * 4];
        let got = GetDIBits(
            mem_dc,
            bitmap,
            0,
            height as u32,
            Some(buf.as_mut_ptr() as *mut _),
            &mut bmi,
            DIB_RGB_COLORS,
        );

        SelectObject(mem_dc, old);
        let _ = DeleteObject(HGDIOBJ(bitmap.0));
        let _ = DeleteDC(mem_dc);
        ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);

        if got == 0 {
            return Err(AgentError::Internal("GetDIBits failed".into()));
        }

        // BGRA -> RGBA
        for chunk in buf.chunks_exact_mut(4) {
            chunk.swap(0, 2);
        }
        encode_png(&buf, width as u32, height as u32)
    }
}

#[cfg(target_os = "windows")]
fn encode_png(rgba: &[u8], width: u32, height: u32) -> Result<Vec<u8>> {
    use std::io::Write;
    // Minimal PNG encoder using std::io and the zlib stream embedded in the
    // `zip` crate via deflate. To keep dependencies small we hand-roll the
    // PNG chunks and use the zlib stream from flate2 via the zip crate's
    // deflate feature is too low-level — instead, fall back to writing an
    // uncompressed-style image is not standards-compliant. We therefore
    // implement compression using `miniz_oxide` via the `zip` crate's
    // dependency tree which exposes a deflate function.

    use std::io::Cursor;
    let mut out: Vec<u8> = Vec::new();
    // PNG signature
    out.extend_from_slice(&[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]);

    // IHDR
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.push(8); // bit depth
    ihdr.push(6); // color type RGBA
    ihdr.push(0); // compression
    ihdr.push(0); // filter
    ihdr.push(0); // interlace
    write_chunk(&mut out, *b"IHDR", &ihdr);

    // Image data — apply filter byte 0 per row, then zlib-compress.
    let row_bytes = (width as usize) * 4;
    let mut raw: Vec<u8> = Vec::with_capacity((row_bytes + 1) * height as usize);
    for y in 0..height as usize {
        raw.push(0);
        let start = y * row_bytes;
        raw.extend_from_slice(&rgba[start..start + row_bytes]);
    }
    let compressed = zlib_compress(&raw)?;
    write_chunk(&mut out, *b"IDAT", &compressed);
    write_chunk(&mut out, *b"IEND", &[]);
    let _ = Cursor::new(&out);
    let _ = Write::flush;
    Ok(out)
}

#[cfg(target_os = "windows")]
fn write_chunk(out: &mut Vec<u8>, kind: [u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    let start = out.len();
    out.extend_from_slice(&kind);
    out.extend_from_slice(data);
    let crc = crc32(&out[start..]);
    out.extend_from_slice(&crc.to_be_bytes());
}

#[cfg(target_os = "windows")]
fn crc32(data: &[u8]) -> u32 {
    let mut table = [0u32; 256];
    for n in 0..256u32 {
        let mut c = n;
        for _ in 0..8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
        }
        table[n as usize] = c;
    }
    let mut c = 0xFFFF_FFFFu32;
    for &b in data {
        c = table[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    c ^ 0xFFFF_FFFF
}

#[cfg(target_os = "windows")]
fn zlib_compress(input: &[u8]) -> Result<Vec<u8>> {
    // Use deflate from the `zip` dependency's miniz_oxide reexport. To avoid
    // hard-pinning the API, we re-implement a minimal zlib wrapper using
    // stored (uncompressed) deflate blocks. This is standards-compliant but
    // yields larger PNGs; acceptable for low-bandwidth diagnostic captures.
    let mut out = Vec::with_capacity(input.len() + input.len() / 65535 * 5 + 8);
    out.push(0x78); // CMF
    out.push(0x01); // FLG — no compression, fastest
    let mut i = 0;
    while i < input.len() {
        let remaining = input.len() - i;
        let block = remaining.min(65535);
        let final_block = (i + block) == input.len();
        out.push(if final_block { 0x01 } else { 0x00 });
        out.extend_from_slice(&(block as u16).to_le_bytes());
        out.extend_from_slice(&(!(block as u16)).to_le_bytes());
        out.extend_from_slice(&input[i..i + block]);
        i += block;
    }
    // Adler-32 checksum
    let mut a: u32 = 1;
    let mut b: u32 = 0;
    for &byte in input {
        a = (a + byte as u32) % 65521;
        b = (b + a) % 65521;
    }
    let adler = (b << 16) | a;
    out.extend_from_slice(&adler.to_be_bytes());
    Ok(out)
}

#[cfg(target_os = "windows")]
async fn show_consent_dialog() -> Result<bool> {
    // For unattended LocalSystem service contexts, no interactive desktop is
    // available — treat as denial unless we're running with a console.
    if std::env::var_os("SESSIONNAME").is_none() {
        return Ok(false);
    }
    tokio::task::spawn_blocking(|| -> bool {
        use windows::core::PCWSTR;
        use windows::Win32::UI::WindowsAndMessaging::{
            MessageBoxW, IDYES, MB_ICONQUESTION, MB_YESNO, MB_TOPMOST,
        };

        fn to_w(s: &str) -> Vec<u16> {
            s.encode_utf16().chain(std::iter::once(0)).collect()
        }

        unsafe {
            let title = to_w("Helen Agent — Screen Capture");
            let text = to_w(
                "An administrator is requesting a screen capture of this device.\nAllow?",
            );
            let res = MessageBoxW(
                None,
                PCWSTR(text.as_ptr()),
                PCWSTR(title.as_ptr()),
                MB_YESNO | MB_ICONQUESTION | MB_TOPMOST,
            );
            res == IDYES
        }
    })
    .await
    .map_err(|e| AgentError::Internal(e.to_string()))
}
