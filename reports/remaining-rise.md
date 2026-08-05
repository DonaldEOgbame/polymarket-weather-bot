# Remaining-rise climatology

Fitted on 12 months of METAR observations from 10 stations spanning the traded climate zones.

`f(h)` = fraction of the day's diurnal range still to RISE after local hour h — used to condition a daily MAX on observations so far.
`g(h)` = fraction still to FALL — used for daily MIN.

Dimensionless on purpose: a °F table fitted on August in Chicago is nonsense for August in Wellington. At runtime these are multiplied by the diurnal range the ensemble forecasts for the day.

| local hour | f mean | f sd | g mean | g sd | n days |
|---|---|---|---|---|---|
| 00 | 0.697 | 0.242 | 0.279 | 0.236 | 3,611 |
| 01 | 0.686 | 0.242 | 0.227 | 0.223 | 3,616 |
| 02 | 0.679 | 0.241 | 0.185 | 0.209 | 3,617 |
| 03 | 0.674 | 0.240 | 0.145 | 0.194 | 3,617 |
| 04 | 0.670 | 0.240 | 0.112 | 0.184 | 3,617 |
| 05 | 0.666 | 0.239 | 0.086 | 0.172 | 3,618 |
| 06 | 0.658 | 0.237 | 0.068 | 0.162 | 3,618 |
| 07 | 0.629 | 0.229 | 0.059 | 0.153 | 3,618 |
| 08 | 0.543 | 0.214 | 0.056 | 0.146 | 3,618 |
| 09 | 0.415 | 0.198 | 0.053 | 0.142 | 3,618 |
| 10 | 0.285 | 0.176 | 0.051 | 0.138 | 3,618 |
| 11 | 0.179 | 0.154 | 0.049 | 0.134 | 3,618 |
| 12 | 0.105 | 0.126 | 0.047 | 0.130 | 3,618 |
| 13 | 0.054 | 0.096 | 0.045 | 0.126 | 3,618 |
| 14 | 0.026 | 0.072 | 0.043 | 0.123 | 3,618 |
| 15 | 0.012 | 0.056 | 0.041 | 0.119 | 3,618 |
| 16 | 0.006 | 0.043 | 0.039 | 0.115 | 3,618 |
| 17 | 0.004 | 0.036 | 0.037 | 0.110 | 3,618 |
| 18 | 0.003 | 0.030 | 0.033 | 0.101 | 3,618 |
| 19 | 0.002 | 0.027 | 0.029 | 0.091 | 3,618 |
| 20 | 0.002 | 0.024 | 0.023 | 0.078 | 3,618 |
| 21 | 0.001 | 0.018 | 0.016 | 0.059 | 3,618 |
| 22 | 0.001 | 0.011 | 0.009 | 0.043 | 3,618 |
| 23 | 0.000 | 0.000 | 0.000 | 0.000 | 3,618 |

## Per-station f(h) — is pooling defensible?

If these disagree materially the pooled table is wrong and the climatology needs a per-climate key.

| station | zone | days | 00 | 06 | 09 | 12 | 15 | 18 | 21 |
|---|---|---|---|---|---|---|---|---|---|
| KORD | continental | 362 | 0.68 | 0.63 | 0.38 | 0.12 | 0.02 | 0.01 | 0.00 |
| KMIA | tropical maritime | 364 | 0.76 | 0.73 | 0.38 | 0.08 | 0.00 | 0.00 | 0.00 |
| KLAX | Mediterranean coastal | 365 | 0.77 | 0.73 | 0.35 | 0.04 | 0.00 | 0.00 | 0.00 |
| KBKF | high plains | 350 | 0.74 | 0.69 | 0.37 | 0.09 | 0.01 | 0.00 | 0.00 |
| EGLC | maritime temperate | 360 | 0.64 | 0.60 | 0.44 | 0.15 | 0.02 | 0.01 | 0.00 |
| RJTT | humid subtropical monsoon | 363 | 0.64 | 0.59 | 0.39 | 0.11 | 0.02 | 0.00 | 0.00 |
| ZGGG | subtropical monsoon | 365 | 0.71 | 0.69 | 0.50 | 0.15 | 0.01 | 0.00 | 0.00 |
| WSSS | equatorial | 365 | 0.71 | 0.67 | 0.44 | 0.12 | 0.01 | 0.00 | 0.00 |
| OEJN | desert | 365 | 0.72 | 0.70 | 0.52 | 0.07 | 0.00 | 0.00 | 0.00 |
| NZWN | southern maritime | 359 | 0.61 | 0.54 | 0.37 | 0.11 | 0.02 | 0.01 | 0.00 |
