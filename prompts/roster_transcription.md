# Crew roster transcription contract

Transcribe all submitted sources as **one candidate roster**. Prefer usable PDF
text supplied in the request; otherwise inspect every PDF page/image. Return
JSON only, matching this shape:

```json
{
  "schema_version": 1,
  "coverage": "FULL",
  "report_header": {
    "period_from": "01Aug37",
    "period_to": "01Oct37",
    "port_local_notice_present": true
  },
  "rows": [
    {
      "source_index": 0,
      "row_index": 14,
      "start_date": "06Aug37",
      "day": "Thu",
      "flight_number": "ZX 410",
      "sector": "SIN-TPE",
      "duty": "FLY",
      "rpt": "0945",
      "std": "1145",
      "sta": "1630",
      "flight_time": "04:45",
      "remarks": null,
      "unreadable": []
    }
  ]
}
```

`coverage` is `FULL`, `PARTIAL`, or `UNCERTAIN`. Preserve printed strings and
blank cells (`null`). Never infer missing dates, values, duties, routes, or
times. List doubtful field names in `unreadable`. Preserve visual row order
using `source_index` and `row_index`. Do not normalize flight numbers, sectors,
dates, timezones, duties, or day rollovers. Python owns all interpretation.
