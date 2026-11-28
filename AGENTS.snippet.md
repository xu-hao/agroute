Before delegating an authorized task to a worker, invoke the installed `agroute` and `howhard` skills to inspect live
quota, select an eligible route, and honor `needs_review` and the native or cross-agent execution plan; report
unavailable routing or exhausted quota instead of bypassing them. Afterward, record the truthful outcome and measured
usage in `howhard` memory. Assigned leaf workers execute their task directly, without recursive delegation.
