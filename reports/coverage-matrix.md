# Open-Meteo coverage matrix

> **PARTIAL — 21 of 32 candidate models.** The remaining eleven are
> European and Asian limited-area models, needed only for Phase 2.5
> (per-city `extra_models`). A limited-area model costs 51 individual
> probes because the per-coordinate fallback fires on every
> out-of-domain 400, so the tail of the run is slow. Re-run
> `python coverage_matrix.py`; results are checkpointed per model.

22 candidate models x 51 cities, probed ONE MODEL AT A TIME.

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
| `ukmo_uk_deterministic_2km` | 0/51 | 0 | 0 | 0 | NO DATA ANYWHERE (statuses: bad_json) |
| `meteofrance_arpege_europe` | 14/51 | 14 | 14 | 14 | limited domain — 37 cities null |
| `meteofrance_arome_france_hd` | 7/51 | 7 | 7 | 0 | limited domain — 44 cities null; horizon-limited (72h at 0 vs 24h at 7) |

## Per-city availability

| city | models with data |
|---|---|
| NYC | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_hrdps_continental, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Chicago | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_hrdps_continental, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Miami | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Dallas | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Los Angeles | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| San Francisco | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Austin | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Houston | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Seattle | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_hrdps_continental, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Denver | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Atlanta | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Toronto | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_hrdps_continental, gem_regional, gfs_global, gfs_hrrr, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ncep_nam_conus, ncep_nbm_conus, ukmo_global_deterministic_10km |
| Mexico City | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Panama | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Buenos Aires | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Sao Paulo | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| London | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Paris | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Berlin | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Amsterdam | 15: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Helsinki | 13: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Istanbul | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Madrid | 13: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Milan | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Moscow | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Munich | 14: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_d2, icon_eu, icon_global, jma_gsm, meteofrance_arome_france_hd, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Warsaw | 13: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gem_regional, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Tel Aviv | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Ankara | 12: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_eu, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Jeddah | 11: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_europe, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Lagos | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Cape Town | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Tokyo | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Hong Kong | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Seoul | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Shanghai | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Beijing | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Guangzhou | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Shenzhen | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Chengdu | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Chongqing | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Wuhan | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Qingdao | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Busan | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Taipei | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Singapore | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Kuala Lumpur | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Jakarta | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Manila | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Lucknow | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
| Wellington | 10: cma_grapes_global, ecmwf_aifs025_single, ecmwf_ifs025, gem_global, gfs_global, icon_global, jma_gsm, meteofrance_arpege_world, ncep_aigfs025, ukmo_global_deterministic_10km |
