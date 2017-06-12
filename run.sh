#!/bin/bash

python gen_requests.py && \
python otpprofiler.py localhost:8080 && \
python hreport.py run_summary.*json > report.html && \
python -m SimpleHTTPServer



