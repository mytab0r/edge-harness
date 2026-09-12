#!/usr/bin/env bash
# Гвардия периодической саморевизии конвейера (issue #1025,
# scripts/orchestra/self_review.py): жёсткий фильтр факта, дедуп по
# отпечатку/токенной похожести, суточный потолок и fail-loud на отказ
# транспорта — все чистые функции, сеть не нужна.
#
# Доказательство мутацией (см. докстринг test_self_review.py): снять тело
# has_verifiable_fact (return True) красит 3 теста; снять тело fingerprint
# (константа) красит test_fingerprint_differs_by_class — оба возврата
# документированы там же, не только здесь.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/orchestra/test_self_review.py -q
