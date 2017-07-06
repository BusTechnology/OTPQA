#!/bin/bash

python gen_requests.py && \
python otpprofiler.py beta.planmytrip.nyc && \
python hreport.py run_summary.*json > report.html && \
python -m SimpleHTTPServer



