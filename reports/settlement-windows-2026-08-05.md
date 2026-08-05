# Settlement-window audit — 2026-08-05

52 stations, 20,988 live temperature markets read.

- **48 classified** — 48 `local`
- **0 UNKNOWN** (text is ambiguous — excluded from trading)
- **4 unaudited** (no live temperature market right now — not the same thing; see below)

## Verdict

**Every city with a live temperature market settles on the LOCAL calendar day.** The resolution text is near-verbatim identical across all of them: *"the highest temperature recorded for all times on this day for the <STATION> Station"*. No market in the population uses 00-24Z, and none uses 6-hourly synoptic groups.

This is the finding the phase was for, and it is a negative one: the day-boundary hypothesis behind Hong Kong, Moscow, Seoul and London was right about the STATION and wrong about the WINDOW. Those were station-identity bugs, not day-boundary bugs. `metar.fetch_day_extremes` already filters observations to the station's local calendar day using its IANA timezone, which is exactly what the text specifies — so the reader and the resolver already agree, for all 48 classified cities.

Three settlement sources appear, all reporting a local day:

| source | cities | what it publishes |
|---|---|---|
| Wunderground daily history | most | its own rollup of the station METAR feed |
| NOAA `weather.gov/wrh/timeseries` | Moscow, Istanbul, Tel Aviv | hourly obs, highest reading in the "Temp" column |
| HKO Daily Extract | Hong Kong | "Absolute Daily Max", one decimal |

## Per-city

| city | window | markets | evidence | source |
|---|---|---|---|---|
| Amsterdam | local | 385 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Amsterdam Airport Schiphol Station, available here: h... | wunderground |
| Ankara | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Esenboğa Intl Airport Station, available here: https:... | wunderground |
| Atlanta | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Hartsfield-Jackson International Airport Station, ava... | wunderground |
| Austin | local | 363 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Austin-Bergstrom International Airport Station, avail... | wunderground |
| Beijing | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Beijing Capital International Airport Station, availa... | wunderground |
| Berlin | NO_MARKET | 0 | no live temperature market during the audit — unaudited, not ambiguous; re-run when one opens | — |
| Buenos Aires | local | 363 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Minister Pistarini Intl Airport Station, available he... | wunderground |
| Busan | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Gimhae Intl Airport Station, available here: https://... | wunderground |
| Cape Town | local | 385 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Cape Town International Airport Station, available he... | wunderground |
| Chengdu | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Chengdu Shuangliu International Airport Station, avai... | wunderground |
| Chicago | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Chicago O'Hare Intl Airport Station, available here: ... | wunderground |
| Chongqing | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Chongqing Jiangbei International Airport Station, ava... | wunderground |
| Dallas | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Dallas Love Field Station, available here: https://ww... | wunderground |
| Denver | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Buckley Space Force Base Station, available here: htt... | wunderground |
| Guangzhou | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Guangzhou Baiyun International Airport Station, avail... | wunderground |
| Helsinki | local | 385 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Helsinki Vantaa Airport Station, available here: http... | wunderground |
| Hong Kong | local | 748 | C)" the specified date once information is finalized in the relevant "Daily Extract", available here: https://www.weather.gov.hk/en/cis/climat.htm This m | weather.gov |
| Houston | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the William P. Hobby Airport Station, available here: htt... | wunderground |
| Istanbul | local | 385 | n from NOAA, specifically the highest reading under the "Temp" column for all times on this day, available here: https://www.weather.gov/wrh/timeseries?site=... | weather.gov |
| Jakarta | NO_MARKET | 0 | no live temperature market during the audit — unaudited, not ambiguous; re-run when one opens | — |
| Jeddah | local | 385 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the King Abdulaziz International Airport Station, availab... | wunderground |
| Kuala Lumpur | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Kuala Lumpur Intl Airport Station, available here: ht... | wunderground |
| Lagos | NO_MARKET | 0 | no live temperature market during the audit — unaudited, not ambiguous; re-run when one opens | — |
| London | local | 748 | ation from Wunderground, specifically the lowest temperature recorded for all times on this day for the London City Airport Station, available here: https://... | wunderground |
| Los Angeles | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Los Angeles International Airport Station, available ... | wunderground |
| Lucknow | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Chaudhary Charan Singh Intl Airport Station, availabl... | wunderground |
| Madrid | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Adolfo Suárez Madrid-Barajas Airport Station, availab... | wunderground |
| Manila | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Ninoy Aquino International Airport Station, available... | wunderground |
| Mexico City | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Benito Juárez International Airport Station, availabl... | wunderground |
| Miami | local | 748 | ation from Wunderground, specifically the lowest temperature recorded for all times on this day for the Miami Intl Airport Station, available here: https://w... | wunderground |
| Milan | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Malpensa Intl Airport Station, available here: https:... | wunderground |
| Moscow | local | 385 | n from NOAA, specifically the highest reading under the "Temp" column for all times on this day, available here: https://www.weather.gov/wrh/timeseries?site=... | weather.gov |
| Munich | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Munich Airport Station, available here: https://www.w... | wunderground |
| NYC | NO_MARKET | 0 | no live temperature market during the audit — unaudited, not ambiguous; re-run when one opens | — |
| New York | local | 748 | ation from Wunderground, specifically the lowest temperature recorded for all times on this day for the LaGuardia Airport Station, available here: https://ww... | wunderground |
| Panama | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Marcos A. Gelabert Intl Airport Station, available he... | wunderground |
| Paris | local | 748 | ation from Wunderground, specifically the lowest temperature recorded for all times on this day for the Paris-Le Bourget Airport Station, available here: htt... | wunderground |
| Qingdao | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Qingdao Jiaodong International Airport Station, avail... | wunderground |
| San Francisco | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the San Francisco International Airport Station, availabl... | wunderground |
| Sao Paulo | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Sao Paulo-Guarulhos International Airport Station, av... | wunderground |
| Seattle | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Seattle-Tacoma International Airport Station, availab... | wunderground |
| Seoul | local | 748 | ation from Wunderground, specifically the lowest temperature recorded for all times on this day for the Incheon Intl Airport Station, available here: https:/... | wunderground |
| Shanghai | local | 748 | ation from Wunderground, specifically the lowest temperature recorded for all times on this day for the Shanghai Pudong International Airport Station, availa... | wunderground |
| Shenzhen | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Shenzhen Bao'an International Airport Station, availa... | wunderground |
| Singapore | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Singapore Changi Airport Station, available here: htt... | wunderground |
| Taipei | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Taipei Songshan Airport Station, available here: http... | wunderground |
| Tel Aviv | local | 374 | n from NOAA, specifically the highest reading under the "Temp" column for all times on this day, available here: https://www.weather.gov/wrh/timeseries?site=... | weather.gov |
| Tokyo | local | 748 | ation from Wunderground, specifically the lowest temperature recorded for all times on this day for the Tokyo Haneda Airport Station, available here: https:/... | wunderground |
| Toronto | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Toronto Pearson Intl Airport Station, available here:... | wunderground |
| Warsaw | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Warsaw Chopin Airport Station, available here: https:... | wunderground |
| Wellington | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Wellington Intl Airport Station, available here: http... | wunderground |
| Wuhan | local | 374 | tion from Wunderground, specifically the highest temperature recorded for all times on this day for the Wuhan Tianhe International Airport Station, available... | wunderground |

## SPECI and corrections

No market description in the sampled population mentions SPECI reports or later-issued corrections. The text says 'highest temperature recorded' and leaves the question open.

What our reader does, for the record: `metar.fetch_day_extremes` requests `data=tmpc` from the IEM ASOS archive, which returns **all** observation rows for the station — routine METAR and SPECI alike — so an off-hour special issued because the temperature spiked IS included. IEM serves the archive's current content, so a correction re-issued later is picked up on any refetch after it lands; the in-process cache keys on (icao, date) and only caches days that are complete, so a correction arriving within the 2h grace window is still seen.

## max_tmpf vs max(hourly)

We use `max(hourly observations)`, not the IEM daily-summary `max_tmpf`. They differ on frontal days, when the true peak falls between two observation times: the daily summary captures it, the hourly max does not.

This matters most where the settlement source publishes its own daily rollup. Hong Kong already has that handled — HKO's 'Absolute Daily Max' is read directly. Wunderground-settled cities are the open question: Wunderground publishes a daily history page whose maximum is its own rollup of the same METAR feed, and whether that rollup equals our max(obs) on a frontal day has not been verified against a resolved market.

## Excluded from trading

None. No city's resolution text was ambiguous.

## Unaudited (no live temperature market)

Not excluded. "The text does not say" and "there is no market open right now" are different states, and only the first justifies refusing to trade — excluding on the second would permanently drop cities for being out of season. These are classified automatically the next time the audit runs against an open market.

- Berlin
- Jakarta
- Lagos
- NYC
