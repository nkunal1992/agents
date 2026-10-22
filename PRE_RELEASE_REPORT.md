# Pre-release report — #212

| check                      | verdict | detail |
|---|---|---|
| sharepoint-knowledge:wheel | FAIL | pip install failed: 0.2rc4, 0.10.2, 0.10.3rc2)

[notice] A new release of pip is available: 26.2.1 -> 26.2.2
[notice] To update, run: /private/var/folders/w0/pxdhcqbx4qnggmfv68d6tvdm0000gn/T/tmplcehokjd/v/bin/python3.13 -m pip install --upgrade pip
ERROR: No matching distribution found for confluence-knowledge~=0.11.0
 |
| local-knowledge:wheel | FAIL | pip install failed: 0.2rc4, 0.10.2, 0.10.3rc2)

[notice] A new release of pip is available: 26.2.1 -> 26.2.2
[notice] To update, run: /private/var/folders/w0/pxdhcqbx4qnggmfv68d6tvdm0000gn/T/tmpsw5hrslh/v/bin/python3.13 -m pip install --upgrade pip
ERROR: No matching distribution found for confluence-knowledge~=0.11.0
 |
| confluence-knowledge:wheel | PASS | install=confluence_knowledge-0.11.0-py3-none-any.whl import=s/w0/pxdhcqbx4qnggmfv68d6tvdm0000gn/T/tmp17lfqmjj/v/lib/python3.13/site-packages |
| engine:e2e_real_data | FAIL | real-data journey (shared daemon, canary, citations) |
| cloud:e2e_cloud_real | FAIL | real Cloud journey (scoped subtree 524289) |

**verdict: RED — 4 check(s) failed**
