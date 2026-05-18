# Windows PE version metadata for CommClient-Server.exe
# Loaded by PyInstaller via `eval()`, so this file must contain a single
# top-level VSVersionInfo() expression — no docstring, no other statements.
# To bump the version: edit the (1, 0, 0, 0) tuples and rebuild.
# fmt: off
VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=(1, 0, 0, 0),
        prodvers=(1, 0, 0, 0),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,          # VOS_NT_WINDOWS32
        fileType=0x1,        # VFT_APP
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable(
                '040904B0',  # US English, Unicode
                [
                    StringStruct('CompanyName',      'CommClient Team'),
                    StringStruct('FileDescription',  'CommClient LAN Communication Server'),
                    StringStruct('FileVersion',      '1.0.0.0'),
                    StringStruct('InternalName',     'CommClient-Server'),
                    StringStruct('LegalCopyright',   'Copyright (C) 2024-2026 CommClient Team'),
                    StringStruct('OriginalFilename', 'CommClient-Server.exe'),
                    StringStruct('ProductName',      'CommClient'),
                    StringStruct('ProductVersion',   '1.0.0.0'),
                ],
            ),
        ]),
        VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])]),
    ],
)
