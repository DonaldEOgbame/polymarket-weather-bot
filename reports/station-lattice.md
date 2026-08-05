# Station reporting lattice

51 stations. 40 `C`, 10 `F`, 1 `UNKNOWN`

## Why this matters

`metar.resolved_extreme_f` rounded EVERY city's reading to a whole °C before converting to °F. For a station that reports in whole °F that is a round trip through a coarser grid than the observation itself: 27.78°C is exactly 82°F, rounds to 28°C, and comes back as 82.4°F. US markets use 2°F-wide buckets, so an error up to 0.9°F decides outcomes.

The lattice also determines which buckets are REACHABLE. No impossible bucket has ever been observed, and this explains why rather than leaving it to luck: US markets quote °F against °F-reporting stations, and the rest quote °C against °C-reporting stations, so in both cases the market's unit is the station's unit and every bucket contains at least one reachable value.

| station | cities | lattice | % on °C | % on °F | distinct fractions | n |
|---|---|---|---|---|---|---|
| CYYZ | Toronto | C | 100.0 | 21.6 | 1 | 204 |
| DNMM | Lagos | C | 100.0 | 26.2 | 1 | 145 |
| EDDB | Berlin | C | 100.0 | 26.2 | 1 | 336 |
| EDDM | Munich | C | 100.0 | 21.4 | 1 | 336 |
| EFHK | Helsinki | C | 100.0 | 27.4 | 1 | 336 |
| EGLC | London | C | 100.0 | 21.4 | 1 | 336 |
| EHAM | Amsterdam | C | 100.0 | 21.4 | 1 | 336 |
| EPWA | Warsaw | C | 100.0 | 19.6 | 1 | 336 |
| FACT | Cape Town | C | 100.0 | 20.4 | 1 | 226 |
| KATL | Atlanta | F | 14.4 | 99.0 | 9 | 194 |
| KAUS | Austin | F | 11.3 | 100.0 | 9 | 186 |
| KBKF | Denver | **UNKNOWN** | 22.0 | 31.3 | 14 | 182 |
| KDAL | Dallas | F | 12.5 | 100.0 | 9 | 168 |
| KHOU | Houston | F | 11.2 | 100.0 | 9 | 169 |
| KLAX | Los Angeles | F | 23.5 | 98.0 | 9 | 204 |
| KLGA | NYC, New York | F | 13.3 | 97.5 | 9 | 203 |
| KMIA | Miami | F | 8.9 | 99.5 | 9 | 192 |
| KORD | Chicago | F | 11.7 | 98.9 | 9 | 188 |
| KSEA | Seattle | F | 10.2 | 100.0 | 9 | 177 |
| KSFO | San Francisco | F | 15.2 | 100.0 | 9 | 184 |
| LEMD | Madrid | C | 100.0 | 14.6 | 1 | 336 |
| LFPB | Paris | C | 100.0 | 19.0 | 1 | 336 |
| LIMC | Milan | C | 100.0 | 30.4 | 1 | 336 |
| LLBG | Tel Aviv | C | 100.0 | 15.5 | 1 | 336 |
| LTAC | Ankara | C | 100.0 | 16.4 | 1 | 336 |
| LTFM | Istanbul | C | 100.0 | 17.9 | 1 | 336 |
| MMMX | Mexico City | C | 100.0 | 23.1 | 1 | 255 |
| MPMG | Panama | C | 100.0 | 10.9 | 1 | 175 |
| NZWN | Wellington | C | 100.0 | 17.9 | 1 | 336 |
| OEJN | Jeddah | C | 100.0 | 14.3 | 1 | 168 |
| RCSS | Taipei | C | 100.0 | 27.2 | 1 | 305 |
| RJTT | Tokyo | C | 100.0 | 28.0 | 1 | 336 |
| RKPK | Busan | C | 100.0 | 19.7 | 1 | 173 |
| RKSI | Seoul | C | 100.0 | 13.4 | 1 | 336 |
| RPLL | Manila | C | 100.0 | 11.6 | 1 | 173 |
| SAEZ | Buenos Aires | C | 100.0 | 21.7 | 1 | 203 |
| SBGR | Sao Paulo | C | 100.0 | 21.3 | 1 | 174 |
| UUWW | Moscow | C | 100.0 | 23.8 | 1 | 391 |
| VHHH | Hong Kong | C | 100.0 | 12.8 | 1 | 446 |
| VILK | Lucknow | C | 100.0 | 15.8 | 1 | 335 |
| WIII | Jakarta | C | 100.0 | 9.5 | 1 | 336 |
| WMKK | Kuala Lumpur | C | 100.0 | 15.8 | 1 | 341 |
| WSSS | Singapore | C | 100.0 | 16.2 | 1 | 339 |
| ZBAA | Beijing | C | 100.0 | 22.2 | 1 | 361 |
| ZGGG | Guangzhou | C | 100.0 | 24.0 | 1 | 367 |
| ZGSZ | Shenzhen | C | 100.0 | 6.5 | 1 | 168 |
| ZHHH | Wuhan | C | 100.0 | 22.6 | 1 | 168 |
| ZSPD | Shanghai | C | 100.0 | 24.5 | 1 | 343 |
| ZSQD | Qingdao | C | 100.0 | 20.8 | 1 | 168 |
| ZUCK | Chongqing | C | 100.0 | 26.8 | 1 | 168 |
| ZUUU | Chengdu | C | 100.0 | 16.7 | 1 | 168 |

## UNKNOWN lattice

Not guessed. Rounding a reading onto a grid it does not live on is the bug this audit found; doing it on a hunch would be the same bug with less evidence. These fall back to the raw reading, unrounded.

- KBKF (Denver): mixed lattice
