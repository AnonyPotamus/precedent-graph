# Graph data: attribution and licence

`precedents_full.json.xz` and `precedents_compact.json.gz` are an adaptation of
**Indian Supreme Court Judgments** by Pradeep Vanga / Dattam Labs
(https://github.com/vanga/indian-supreme-court-judgments,
https://registry.opendata.aws/indian-supreme-court-judgments/),
licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
Original source: Supreme Court of India, Digital SCR (https://scr.sci.gov.in).

Changes: judgment PDFs were converted to text and parsed into structured fields
(bench, statutes, keywords, headnotes, holdings, cited cases and their editorial
treatments). Citations were normalised and matched, and the result was assembled
into a citation graph with computed PageRank and community scores.

These files are shared under CC BY 4.0. If you reuse them, credit the original
dataset as above and indicate any changes you make. No endorsement by the
dataset authors or the Supreme Court of India is implied.

Citation requested by the dataset authors:

    Vanga (2025). Indian Supreme Court Judgments. [Data set]. AWS Open Data Registry.
    https://registry.opendata.aws/indian-supreme-court-judgments/

The code in this repository is MIT-licensed; that licence does not cover these data files.
