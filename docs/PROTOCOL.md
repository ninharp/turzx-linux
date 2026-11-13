# TURZX 8.8" USB protocol and theme format

Source: TURZX V3.1.0 (`TURZX.exe` = `UsbMonitorL.exe` 4.2.1.3, .NET Framework 4.7.2,
Dotfuscator). Decompiled with ILSpy; metadata cross-checked with REA. String
literals are encrypted with `_A839.b(s, k)`: per UTF-16 code unit, low byte ^ key++,
high byte ^ key++, then swap, with `key = 1172786343 + k + 38` (low byte used).

## USB

* Device `1cbe:0088` "TURZX1.0", high speed, vendor class, one interface,
  bulk EP 0x01 OUT / 0x81 IN (512 bytes). Behind an internal hub `1a86:8091`
  together with `1cbe:f000` "USB-Daemon" (not used by the app).
* App class: `꾆` (LibUsbDotNet), device wrapper `ꭚ : AbDevice`.
  Device table: `Version.Dev_88inch_win207lcd` (480×1920, `Is207Lcd`, `IsPotrit`, `DesktopMode`).

### Command block

```
plain[500]: [0]=cmd [1]=0 [2]=0x1A [3]=0x6D [4..7]=LE uint32 ms since (yesterday 00:00)
            [8..499]=payload
enc = DES-CBC(key=IV="slv3tuzx", PKCS7)(plain)       -> 504 bytes
block[512] = enc + zeros; block[510]=0xA1; block[511]=0x1A
```

Data commands put a BE uint32 length in payload[0..3] and append the data
directly after the 512-byte block, in the same bulk transfer. Every command is
answered with a 512-byte packet (byte 0 = cmd echo) followed by a zero-length
packet.

| cmd | name in app | meaning | verified |
|---|---|---|---|
| 10 | ꞇ | sync; reply[8..] = firmware string `turzx_0001_0008` | ✔ |
| 11 | _A7E0 | reboot | |
| 12 | _A839 | shutdown | |
| 13 | ꢒ | rotate (payload byte 0..3) | |
| 14 | _A8EB | brightness, payload byte 0..100 | ✔ |
| 15 | ꥄ | video frame rate | |
| 17 | ꦝ | storage/size query (reply[8..11] BE) | |
| 51 | ꮳ | set clock + mode: Y_hi Y_lo M D h m s mode (0 stop, 2 video stream) | ✔ (mode 0) |
| 101 | – | full-screen JPEG (≤ 1 MiB) | ✔ |
| 102 | – | PNG overlay with alpha (≤ 1 MiB), composited over JPEG/video | ✔ |
| 111 | 꼭 | stop video playback | ✔ |
| 123 | 녃 | stop media | ✔ |
| 150 | 뇵 | switch to native desktop mode (then reboot → `1A86:AD11`, Windows IddCx driver `TURZX_display_driver.dll`) | not used |

Measured: JPEG 480×1920 q≈80 (~20 KB) is accepted at > 60 fps, but a burst at
that rate preceded a firmware hang. turzx-linux caps at 30 fps (video) / 20 fps
(desktop) and waits for each reply.

## .turtheme

BinaryFormatter (MS-NRBF) graph of `UsbMonitorL.Theme`: `width`, `height`
(8.8": usually 1920×480), `themePic` (preview only), `videoName`/`videoPath`,
`FrameRate`, `GraphList` (ObservableCollection of `GraphItem` subclasses).
`System.Drawing.Bitmap` fields serialize as PNG in `Data`; `System.Drawing.Color`
as `value` (ARGB, state&2) or `knownColor` (state&1).

Rendering per frame (`Monitor.슥`): transparent canvas of theme size; items in
list order, skipping `Animation` and `hide`. If the theme has a video,
`GraphList[0]` (an Image) becomes the video placeholder (`Theme.녃`). Canvas
rotated 90° CW for the portrait panel. Without video: JPEG q95 via cmd 101;
with video: PNG overlay via cmd 102 while the video plays underneath.

Widget semantics (fonts in pt at 96 dpi, GDI+ text box top = posY with baseline
at OS/2 winAscent, 1/6 em side padding; StatuBar/ArchBar/Clock/Chart formulas)
are implemented in `turzx/render.py` and `turzx/theme.py`.
