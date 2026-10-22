# Pre-release report — #212

| check                      | verdict | detail |
|---|---|---|
| confluence-knowledge:wheel | PASS | install=confluence_knowledge-0.11.0-py3-none-any.whl import=s/w0/pxdhcqbx4qnggmfv68d6tvdm0000gn/T/tmpfkfkpcoo/v/lib/python3.13/site-packages |
| local-knowledge:wheel | FAIL | pip install failed: 0.2rc4, 0.10.2, 0.10.3rc2)

[notice] A new release of pip is available: 26.2.1 -> 26.2.2
[notice] To update, run: /private/var/folders/w0/pxdhcqbx4qnggmfv68d6tvdm0000gn/T/tmptf8es7cp/v/bin/python3.13 -m pip install --upgrade pip
ERROR: No matching distribution found for confluence-knowledge~=0.11.0
 |
| sharepoint-knowledge:wheel | FAIL | pip install failed: 0.2rc4, 0.10.2, 0.10.3rc2)

[notice] A new release of pip is available: 26.2.1 -> 26.2.2
[notice] To update, run: /private/var/folders/w0/pxdhcqbx4qnggmfv68d6tvdm0000gn/T/tmptlnr5_zb/v/bin/python3.13 -m pip install --upgrade pip
ERROR: No matching distribution found for confluence-knowledge~=0.11.0
 |
| engine:e2e_real_data | FAIL | real-data journey (shared daemon, canary, citations) |
| cloud:e2e_cloud_real | FAIL | real Cloud journey (874-page corpus) |

**verdict: RED — 4 check(s) failed**
