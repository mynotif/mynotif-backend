# Access diagnostics deliberately omit URI, query, headers, user and bodies.
# Proxy/App Runner logging still needs a separate qualification before real data.
access_log_format = '%(t)s %(m)s %(s)s %(L)s'
