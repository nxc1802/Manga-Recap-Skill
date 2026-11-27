# V7.1 Media Quality Report

All rendered chapter MP4s and the compiled 10-chapter feature MP4 passed `ffprobe` inspection (`1920x1080`, `h264`, `30/1` fps) and full `ffmpeg -v error -i <file> -f null -` decode (`rc=0`):

- **Ch. 3:** `91.767s` (`DECODE_OK`)
- **Ch. 4:** `69.667s` (`DECODE_OK`)
- **Ch. 5:** `78.867s` (`DECODE_OK`)
- **Ch. 6:** `42.567s`, `14,769,162` bytes (`DECODE_OK`)
- **Ch. 7:** `37.767s`, `19,052,674` bytes (`DECODE_OK`)
- **Ch. 8:** `40.640s`, `15,038,259` bytes (`DECODE_OK`)
- **Ch. 9:** `39.680s`, `14,605,030` bytes (`DECODE_OK`)
- **Ch. 10:** `47.040s`, `21,486,708` bytes (`DECODE_OK`)
- **Compiled 10-Chapter Feature (`v71-accept-kaggle_full_recap.mp4`):** `773.321s`, `257,556,496` bytes (`DECODE_OK`)
