# Exploratory Data Analysis — German Credit Dataset

## 1. Method

- **Registered dataset used:** `german_credit`, as declared in the project's runtime context
  (`.thymira/config.yaml` maps this logical dataset name to a concrete workspace file).
- **Exact declared runtime path it resolved to:** `data/applications.csv` (workspace-relative
  path, as given in the runtime context's "Workspace dataset paths" listing for `german_credit`).
- **Read-only loading:** The file was loaded with `pandas.read_csv("data/applications.csv")` into
  an in-memory DataFrame. No write operations were performed against this file; it was opened only
  for reading, and the source CSV was not modified in any way during this analysis.
- **What was computed:**
  - Overall dataset shape (row and column counts).
  - Per-column dtype (as inferred by pandas), non-null count, missing count, missing percentage,
    and number of distinct non-null values, for all 21 columns.
  - Descriptive summary statistics (`count`, `mean`, `std`, `min`, `25%`, `50%`, `75%`, `max`) for
    all columns pandas identified as numeric.
  - Full value-count distributions (count and percentage of total rows) for all columns pandas
    identified as object/categorical, including explicit counts of missing (`NaN`) entries.
  - A check for the presence of the column `is_high_risk` and, since it was found, its class
    counts and percentages.
  - A scan for constant columns (≤1 distinct value) and high-cardinality columns (>50 distinct
    values).
  - A scan of categorical columns for short string tokens (length ≤ 2 characters) that could
    indicate anomalous or sentinel-like encodings.

No models were trained, no charts or plots were created, and the source dataset file was not
modified — this task performed profiling and statistics only.

## 2. Findings

### 2.1 Dataset shape

- Rows: **998**
- Columns: **21**

### 2.2 Column types table

| Column | dtype | non-null | missing | missing % | distinct (non-null) |
|---|---|---|---|---|---|
| checking_status | object | 998 | 0 | 0.0 | 4 |
| duration_months | float64 | 994 | 4 | 0.4 | 33 |
| credit_history | object | 998 | 0 | 0.0 | 5 |
| purpose | object | 997 | 1 | 0.1 | 10 |
| credit_amount | float64 | 994 | 4 | 0.4 | 916 |
| savings_status | object | 995 | 3 | 0.3 | 5 |
| employment_since | object | 998 | 0 | 0.0 | 5 |
| installment_rate_pct | int64 | 998 | 0 | 0.0 | 4 |
| personal_status_sex | object | 996 | 2 | 0.2 | 4 |
| other_debtors | object | 995 | 3 | 0.3 | 3 |
| residence_since | float64 | 997 | 1 | 0.1 | 4 |
| property_magnitude | object | 995 | 3 | 0.3 | 5 |
| age | float64 | 994 | 4 | 0.4 | 53 |
| other_installment_plans | object | 997 | 1 | 0.1 | 3 |
| housing | object | 996 | 2 | 0.2 | 3 |
| existing_credits | float64 | 993 | 5 | 0.5 | 4 |
| job | object | 997 | 1 | 0.1 | 4 |
| num_dependents | int64 | 998 | 0 | 0.0 | 2 |
| own_telephone | object | 995 | 3 | 0.3 | 2 |
| foreign_worker | object | 995 | 3 | 0.3 | 2 |
| is_high_risk | int64 | 998 | 0 | 0.0 | 2 |

Note: pandas inferred `duration_months`, `credit_amount`, `residence_since`, and `existing_credits`
as `float64` (rather than `int64`), which is consistent with each of these columns containing
missing values in the loaded CSV.

### 2.3 Missing-value summary

Missing values were observed in the following columns (count / percentage of 998 rows):

- `duration_months`: 4 (0.4%)
- `purpose`: 1 (0.1%)
- `credit_amount`: 4 (0.4%)
- `savings_status`: 3 (0.3%)
- `personal_status_sex`: 2 (0.2%)
- `other_debtors`: 3 (0.3%)
- `residence_since`: 1 (0.1%)
- `property_magnitude`: 3 (0.3%)
- `age`: 4 (0.4%)
- `other_installment_plans`: 1 (0.1%)
- `housing`: 2 (0.2%)
- `existing_credits`: 5 (0.5%)
- `job`: 1 (0.1%)
- `own_telephone`: 3 (0.3%)
- `foreign_worker`: 3 (0.3%)

Columns with **no** missing values: `checking_status`, `credit_history`, `employment_since`,
`installment_rate_pct`, `num_dependents`, `is_high_risk`.

### 2.4 Numeric summary statistics

| Column | count | mean | std | min | 25% | 50% | 75% | max |
|---|---|---|---|---|---|---|---|---|
| duration_months | 994 | 20.854125 | 12.016515 | 4.0 | 12.0 | 18.0 | 24.0 | 72.0 |
| credit_amount | 994 | 3263.974849 | 2818.156329 | 250.0 | 1364.5 | 2317.0 | 3970.5 | 18424.0 |
| installment_rate_pct | 998 | 2.971944 | 1.119363 | 1.0 | 2.0 | 3.0 | 4.0 | 4.0 |
| residence_since | 997 | 2.844534 | 1.104437 | 1.0 | 2.0 | 3.0 | 4.0 | 4.0 |
| age | 994 | 35.551308 | 11.394858 | 19.0 | 27.0 | 33.0 | 42.0 | 75.0 |
| existing_credits | 993 | 1.404834 | 0.572608 | 1.0 | 1.0 | 1.0 | 2.0 | 4.0 |
| num_dependents | 998 | 1.154309 | 0.361425 | 1.0 | 1.0 | 1.0 | 1.0 | 2.0 |
| is_high_risk | 998 | 0.300601 | 0.458749 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 |

(`is_high_risk` is the binary target and is included here only because pandas' numeric summary
covers it automatically; its class balance is reported separately in §2.6.)

### 2.5 Categorical distributions

**checking_status** (4 distinct):
- sin_cuenta_corriente: 392 (39.28%)
- menos_de_0_DM: 274 (27.45%)
- 0_a_200_DM: 269 (26.95%)
- 200_DM_o_mas: 63 (6.31%)

**credit_history** (5 distinct):
- pagados_hasta_ahora: 529 (53.01%)
- cuenta_critica_otros_bancos: 292 (29.26%)
- retraso_en_pagos_previos: 88 (8.82%)
- todos_pagados_en_este_banco: 49 (4.91%)
- sin_creditos_previos: 40 (4.01%)

**purpose** (10 distinct + missing):
- radio_tv: 280 (28.06%)
- coche_nuevo: 233 (23.35%)
- mobiliario: 181 (18.14%)
- coche_usado: 102 (10.22%)
- negocio: 96 (9.62%)
- educacion: 50 (5.01%)
- reparaciones: 22 (2.2%)
- electrodomesticos: 12 (1.2%)
- otros: 12 (1.2%)
- reciclaje_profesional: 9 (0.9%)
- missing (NaN): 1 (0.1%)

**savings_status** (5 distinct + missing):
- menos_de_100_DM: 601 (60.22%)
- desconocido_sin_cuenta_ahorro: 181 (18.14%)
- 100_a_500_DM: 102 (10.22%)
- 500_a_1000_DM: 63 (6.31%)
- 1000_DM_o_mas: 48 (4.81%)
- missing (NaN): 3 (0.3%)

**employment_since** (5 distinct):
- 1_a_4_anios: 339 (33.97%)
- 7_anios_o_mas: 252 (25.25%)
- 4_a_7_anios: 173 (17.33%)
- menos_de_1_anio: 172 (17.23%)
- desempleado: 62 (6.21%)

**personal_status_sex** (4 distinct + missing):
- hombre_soltero: 545 (54.61%)
- mujer_divorciada_separada_casada: 309 (30.96%)
- hombre_casado_viudo: 92 (9.22%)
- hombre_divorciado_separado: 50 (5.01%)
- missing (NaN): 2 (0.2%)

**other_debtors** (3 distinct + missing):
- ninguno: 903 (90.48%)
- avalista: 51 (5.11%)
- codeudor: 41 (4.11%)
- missing (NaN): 3 (0.3%)

**property_magnitude** (5 distinct + missing, includes anomalous value):
- coche_u_otro: 328 (32.87%)
- inmueble: 282 (28.26%)
- seguro_vida_o_similar: 231 (23.15%)
- desconocido_sin_propiedad: 153 (15.33%)
- missing (NaN): 3 (0.3%)
- 'd': 1 (0.1%)

**other_installment_plans** (3 distinct + missing):
- ninguno: 811 (81.26%)
- banco: 139 (13.93%)
- tiendas: 47 (4.71%)
- missing (NaN): 1 (0.1%)

**housing** (3 distinct + missing):
- propia: 710 (71.14%)
- alquiler: 178 (17.84%)
- gratuita: 108 (10.82%)
- missing (NaN): 2 (0.2%)

**job** (4 distinct + missing):
- empleado_cualificado: 627 (62.83%)
- no_cualificado_residente: 200 (20.04%)
- directivo_autonomo_alta_cualificacion: 148 (14.83%)
- no_cualificado_no_residente: 22 (2.2%)
- missing (NaN): 1 (0.1%)

**own_telephone** (2 distinct + missing):
- no: 593 (59.42%)
- si: 402 (40.28%)
- missing (NaN): 3 (0.3%)

**foreign_worker** (2 distinct + missing):
- si: 958 (95.99%)
- no: 37 (3.71%)
- missing (NaN): 3 (0.3%)

### 2.6 Target-class balance for `is_high_risk`

The column `is_high_risk` **does exist** in the loaded dataset. Its class distribution is:

- class = 0: 698 rows (69.94%)
- class = 1: 300 rows (30.06%)

Total rows: 998.

## 3. Data-quality limitations

- **Missingness:** 15 of the 21 columns contain missing values, ranging from 0.1% (`purpose`,
  `residence_since`, `other_installment_plans`, `job`) up to 0.5% (`existing_credits`). No column
  exceeds roughly half a percent missing, but the pattern is spread across a majority of columns
  rather than concentrated in one or two.
- **High-cardinality columns:** `credit_amount` (916 distinct non-null values) and `age` (53
  distinct non-null values) were flagged as high-cardinality relative to the other columns in this
  dataset (all other columns have ≤33 distinct values). No constant columns (columns with ≤1
  distinct value) were found.
- **Suspicious / sentinel encoding:** `property_magnitude` contains a single row with the value
  `'d'`, distinct from its other four legitimate category labels (`coche_u_otro`, `inmueble`,
  `seguro_vida_o_similar`, `desconocido_sin_propiedad`). This value could not be mapped to any of
  the other observed categories from the file alone and appears anomalous — it may be a data-entry
  or encoding error, but its true intended meaning cannot be determined without an external data
  dictionary or the data provider.
- **Class imbalance:** The target `is_high_risk` is imbalanced, with the majority class (0) at
  69.94% and the minority class (1) at 30.06% of rows. This is a roughly 2.3:1 ratio and should be
  considered when evaluating any downstream classification model (e.g., accuracy alone would be
  misleading; class-weighting or resampling and threshold-appropriate metrics may be warranted).
- **Column semantics not verifiable from the file alone:** Category labels are in Spanish (e.g.,
  `sin_cuenta_corriente`, `pagados_hasta_ahora`) and their precise numeric/ordinal mapping back to
  the original German Credit dataset's coded attributes (e.g., checking account balance bands,
  credit history codes) cannot be independently confirmed from the CSV contents alone; this
  analysis reports only the labels and frequencies as they appear in the file.
- **No ground-truth validation of "correctness":** This analysis only profiles the file as loaded
  by pandas. It does not verify whether the missing-value patterns are random (MCAR) or systematic
  (e.g., MAR/MNAR), nor whether the anomalous `'d'` value in `property_magnitude` reflects a
  broader data-entry issue elsewhere in the source system — such determinations would require
  information beyond what is contained in this file.

## 4. Artifact location

This report is located at `reports/exploratory-data-analysis.md`.

No models were trained, no charts or plots were created, and the source dataset
(`data/applications.csv`) was not modified as part of producing this report.
