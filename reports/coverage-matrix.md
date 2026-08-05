# Open-Meteo coverage matrix

31 candidate models x 51 cities, probed ONE MODEL AT A TIME.

A multi-model request containing one invalid ID returns HTTP 400 for the whole request, so a batched probe blames every model in the batch. That is where the "GFS unavailable in the Southern Hemisphere" belief came from.

| model | cities with data | 24h | 48h | 72h | notes |
|---|---|---|---|---|---|
| `ecmwf_ifs025` | 51/51 | 51 | 51 | 51 |  |
| `gfs_global` | 51/51 | 51 | 51 | 51 |  |
| `icon_global` | 51/51 | 51 | 51 | 51 |  |
| `gem_global` | 51/51 | 51 | 51 | 51 |  |
| `jma_gsm` | 51/51 | 51 | 51 | 51 |  |
| `ukmo_global_deterministic_10km` | 51/51 | 51 | 51 | 51 |  |
| `meteofrance_arpege_world` | 51/51 | 51 | 51 | 51 |  |
| `bom_access_global` | 0/51 | 0 | 0 | 0 | NO DATA ANYWHERE (statuses: 200) |
| `cma_grapes_global` | 51/51 | 51 | 51 | 51 |  |
| `ecmwf_aifs025_single` | 51/51 | 51 | 51 | 51 |  |
| `gfs_graphcast025` | 0/51 | 0 | 0 | 0 | NO DATA ANYWHERE (statuses: 200) |
| `ncep_aigfs025` | 51/51 | 51 | 51 | 51 |  |
| `ncep_nbm_conus` | 12/51 | 12 | 12 | 12 | limited domain — 39 cities null |
| `gfs_hrrr` | 12/51 | 12 | 12 | 0 | limited domain — 39 cities null; horizon-limited (72h at 0 vs 24h at 12) |
| `ncep_nam_conus` | 12/51 | 12 | 12 | 12 | limited domain — 39 cities null |
| `gem_hrdps_continental` | 4/51 | 4 | 4 | 0 | limited domain — 47 cities null; horizon-limited (72h at 0 vs 24h at 4) |
| `gem_regional` | 20/51 | 20 | 20 | 20 | limited domain — 31 cities null |
| `icon_eu` | 13/51 | 13 | 13 | 13 | limited domain — 38 cities null |
| `icon_d2` | 6/51 | 6 | 6 | 0 | limited domain — 45 cities null; horizon-limited (72h at 0 vs 24h at 6) |
| `ukmo_uk_deterministic_2km` | 0/51 | 0 | 0 | 0 | **INCONCLUSIVE** — 51 cities failed with bad_json; re-probe before concluding anything |
| `meteofrance_arpege_europe` | 14/51 | 14 | 14 | 14 | limited domain — 37 cities null |
| `meteofrance_arome_france_hd` | 7/51 | 7 | 7 | 0 | limited domain — 44 cities null; horizon-limited (72h at 0 vs 24h at 7) |
| `knmi_harmonie_arome_europe` | 0/51 | 0 | 0 | 0 | **INCONCLUSIVE** — 51 cities failed with bad_json; re-probe before concluding anything |
| `knmi_harmonie_arome_netherlands` | 0/51 | 0 | 0 | 0 | **INCONCLUSIVE** — 51 cities failed with bad_json; re-probe before concluding anything |
| `dmi_harmonie_arome_europe` | 0/51 | 0 | 0 | 0 | **INCONCLUSIVE** — 51 cities failed with bad_json; re-probe before concluding anything |
| `metno_seamless` | 51/51 | 51 | 51 | 51 |  |
| `italia_meteo_arpae_icon_2i` | 0/51 | 0 | 0 | 0 | **INCONCLUSIVE** — 51 cities failed with bad_json; re-probe before concluding anything |
| `arpae_cosmo_2i` | 0/51 | 0 | 0 | 0 | NO DATA ANYWHERE (statuses: 400) |
| `jma_msm` | 6/51 | 6 | 6 | 6 | limited domain — 45 cities null |
| `kma_ldps` | 0/51 | 0 | 0 | 0 | **INCONCLUSIVE** — 1 cities failed with 503; re-probe before concluding anything |
| `kma_gdps` | 0/51 | 0 | 0 | 0 | NO DATA ANYWHERE (statuses: 200) |

## Spot-checks resolving the INCONCLUSIVE rows

The bulk run hit Open-Meteo's rate limiter on five models and every city came
back `bad_json` — an HTML error page, not a JSON response. An earlier version of
this report rendered that as "NO DATA ANYWHERE", which was wrong: probed
individually, all five return data.

| model | city | result |
|---|---|---|
| `ukmo_uk_deterministic_2km` | London | 200, 67/96 non-null |
| `knmi_harmonie_arome_europe` | Amsterdam | 200, 77/96 non-null |
| `knmi_harmonie_arome_netherlands` | Amsterdam | 200, 77/96 non-null |
| `dmi_harmonie_arome_europe` | Helsinki | 200, 75/96 non-null |
| `italia_meteo_arpae_icon_2i` | Milan | 200, 85/96 non-null |

`ukmo_uk_deterministic_2km` is the plan's London recommendation, so treating a
transient error as evidence would have removed a member the plan specifically
asked for.

Two rows that ARE definitive, for contrast:

| model | city | result |
|---|---|---|
| `kma_ldps` / `kma_gdps` | Seoul | 200 with **zero** non-null values — serves nothing |
| `arpae_cosmo_2i` | Milan | 400 "ARPAE COSMO models are not available anymore" |

That matters for Phase 2.5: the plan proposes `kma_ldps` and `kma_gdps` for
Seoul and Busan, and neither is available through this endpoint.

The full domain maps for the five INCONCLUSIVE models are still unmeasured.
Re-run `python coverage_matrix.py --models <name>` per model, slowly, to fill
them in.

## Per-city availability

| city | models with data |
|---|---|
| NYC | 16: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_hrdps_continental, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Chicago | 16: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_hrdps_continental, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Miami | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Dallas | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Los Angeles | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| San Francisco | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Austin | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Houston | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Seattle | 16: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_hrdps_continental, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Denver | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Atlanta | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Toronto | 16: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_hrdps_continental, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Mexico City | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Panama | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Buenos Aires | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Sao Paulo | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| London | 16: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Paris | 16: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Berlin | 16: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Amsterdam | 16: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Helsinki | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Istanbul | 13: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Madrid | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Milan | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Moscow | 13: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Munich | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Warsaw | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Tel Aviv | 13: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Ankara | 13: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Jeddah | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Lagos | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Cape Town | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Tokyo | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, jma_msm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Hong Kong | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Seoul | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, jma_msm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Shanghai | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, jma_msm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Beijing | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Guangzhou | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Shenzhen | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Chengdu | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Chongqing | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Wuhan | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Qingdao | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, jma_msm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Busan | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, jma_msm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Taipei | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, jma_msm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Singapore | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Kuala Lumpur | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Jakarta | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Manila | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Lucknow | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
| Wellington | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, metno_seamless, ncep_aigfs025, ukmo_global_deterministic_10km |
