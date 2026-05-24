# Changelog

## [1.0.0] — 2025-05-24

### Added
- Initial public release
- Live `hackrf_sweep` spectrum analyser and waterfall display
- Flask + WebSocket server (single Python file, no static assets)
- Built-in RF simulator for offline demo / no-hardware use
- Digit-dial and direct-type frequency input modes
- Drag-to-pan frequency ruler
- Quick-band presets (FM, Aviation, Marine, Ham 2m, GSM-900, ADS-B)
- LNA / VGA gain sliders with live hardware update
- RF preamp (AMP) toggle
- Bin-width selector (10 kHz – 1 MHz)
- Peak Hold and Averaging trace modes
- Auto-calibrate noise floor (`A` key)
- Frequency marker pins (click on spectrum or waterfall)
- Live hover tooltip with frequency, dBm, and signal name
- Peak signal readout panel
- PNG export of spectrum + waterfall
- Colour palettes: Viridis, Plasma, Inferno, Magma, Rainbow, Hot, Cool, Grayscale
- Keyboard shortcuts: Space, A, R, S, M
- Graceful process management (`kill_hackrf_users`, `free_port`)
- Windows (`taskkill`) and Linux/macOS (`lsof`, `fuser`, `killall`) device management
- Collapsible console log with colour-coded severity levels
- Factory reset button
