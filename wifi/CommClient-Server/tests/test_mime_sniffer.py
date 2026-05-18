"""
Tests for the magic-byte MIME sniffer.

Covers:
  - Positive detection across images, audio, video, archives, documents.
  - Rejection of dangerous payloads (PE / ELF / Mach-O / scripts).
  - Extension vs. detected MIME cross-check.
  - Claimed vs. detected MIME compatibility table.
"""

from __future__ import annotations

import pytest

from app.core.mime_sniffer import (
    DANGEROUS_MIMES,
    is_dangerous,
    matches_extension,
    sniff,
    validate_upload,
)


class TestSniff:
    def test_png(self):
        assert sniff(b"\x89PNG\r\n\x1a\n" + b"rest") == "image/png"

    def test_jpeg(self):
        assert sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 16) == "image/jpeg"

    def test_gif(self):
        assert sniff(b"GIF89a" + b"\x00" * 8) == "image/gif"

    def test_webp(self):
        assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8X") == "image/webp"

    def test_pdf(self):
        assert sniff(b"%PDF-1.7\n") == "application/pdf"

    def test_zip_family(self):
        assert sniff(b"PK\x03\x04") == "application/zip"

    def test_mp3_id3(self):
        assert sniff(b"ID3\x04\x00") == "audio/mpeg"

    def test_mp3_raw_frame(self):
        assert sniff(b"\xff\xfb\x90\x00") == "audio/mpeg"

    def test_wav(self):
        assert sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio/wav"

    def test_mp4(self):
        # ftypisom at offset 4
        assert sniff(b"\x00\x00\x00 ftypisom\x00\x00\x02\x00") == "video/mp4"

    def test_webm(self):
        # EBML magic → matroska (webm is a subset; we currently map to matroska)
        assert sniff(b"\x1a\x45\xdf\xa3\x00\x00\x00\x00").startswith("video/")

    def test_empty_payload(self):
        assert sniff(b"") == "application/octet-stream"

    def test_unknown_binary(self):
        # Random non-text binary garbage
        assert sniff(b"\x00\x01\x02\x03garbage\x00") == "application/octet-stream"

    def test_text_fallback(self):
        assert sniff(b"hello world\n") == "text/plain"

    def test_text_with_nul_is_not_text(self):
        assert sniff(b"hello\x00world") == "application/octet-stream"


class TestDangerousDetection:
    def test_windows_pe(self):
        assert sniff(b"MZ\x90\x00") == "application/x-msdownload"
        assert is_dangerous("application/x-msdownload")

    def test_elf(self):
        assert sniff(b"\x7fELF\x02\x01\x01\x00") == "application/x-executable"
        assert is_dangerous("application/x-executable")

    def test_shellscript(self):
        assert sniff(b"#!/bin/bash\n") == "application/x-shellscript"

    def test_java_class(self):
        assert sniff(b"\xca\xfe\xba\xbe") == "application/java-vm"

    def test_dangerous_set_is_frozen(self):
        assert isinstance(DANGEROUS_MIMES, frozenset)
        assert "application/pdf" not in DANGEROUS_MIMES


class TestExtensionMatch:
    def test_jpeg_extensions(self):
        assert matches_extension("image/jpeg", ".jpg")
        assert matches_extension("image/jpeg", ".jpeg")
        assert matches_extension("image/jpeg", "jpg")  # dotless also accepted
        assert not matches_extension("image/jpeg", ".png")

    def test_case_insensitivity(self):
        assert matches_extension("image/png", ".PNG")

    def test_empty_ext_permissive(self):
        assert matches_extension("image/png", "")
        assert matches_extension("image/png", None)  # type: ignore[arg-type]

    def test_mime_with_no_hints_is_permissive(self):
        # text/plain has no hints registered → should allow any ext
        assert matches_extension("text/plain", ".xyz")


class TestValidateUpload:
    def test_happy_path_png(self):
        head = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        mime, warnings = validate_upload(head, "image/png", ".png")
        assert mime == "image/png"
        assert warnings == []

    def test_mismatched_claim_produces_warning(self):
        # Real PNG, but client claims it's a JPEG
        head = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        mime, warnings = validate_upload(head, "image/jpeg", ".png")
        assert mime == "image/png"
        assert any("mismatch" in w for w in warnings)

    def test_extension_disagrees_with_content(self):
        # Real PDF, but filename says .png
        head = b"%PDF-1.7\n" + b"\x00" * 16
        mime, warnings = validate_upload(head, "application/pdf", ".png")
        assert mime == "application/pdf"
        assert any("extension" in w for w in warnings)

    def test_executable_is_rejected(self):
        head = b"MZ\x90\x00" + b"\x00" * 16
        with pytest.raises(ValueError):
            validate_upload(head, "image/jpeg", ".jpg")

    def test_executable_allowed_when_flag_set(self):
        head = b"MZ\x90\x00" + b"\x00" * 16
        mime, _ = validate_upload(head, "application/octet-stream", ".exe",
                                  allow_dangerous=True)
        assert mime == "application/x-msdownload"

    def test_shellscript_rejected_even_with_txt_claim(self):
        head = b"#!/usr/bin/env sh\n"
        with pytest.raises(ValueError):
            validate_upload(head, "text/plain", ".txt")

    def test_docx_as_zip_is_ok(self):
        # .docx / .xlsx are legitimately zip signatures — must not warn.
        head = b"PK\x03\x04" + b"\x00" * 16
        mime, warnings = validate_upload(head, "application/zip", ".docx")
        assert mime == "application/zip"
        assert warnings == []

    def test_ogg_claimed_vs_audio_ogg_compatible(self):
        head = b"OggS\x00" + b"\x00" * 16
        # Server identified audio/ogg but client said application/ogg — both OK.
        mime, warnings = validate_upload(head, "application/ogg", ".ogg")
        assert mime == "audio/ogg"
        assert warnings == []

    def test_unknown_binary_no_warning(self):
        # Unknown binary → octet-stream, no warning triggered even if
        # claimed_mime disagrees because we can't be sure.
        head = b"\x00\x01\x02garbage" + b"\x00" * 16
        mime, warnings = validate_upload(head, "application/pdf", ".bin")
        assert mime == "application/octet-stream"
        assert warnings == []
