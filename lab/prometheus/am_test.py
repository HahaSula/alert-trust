import lab, json, time
from lab import sh, c
n="amtest"; lab.down(n); lab.up(n, big=1000, slow_delay=0); time.sleep(110)
Q=lambda e: lab.q(n,e)
print('base', [ (r['metric'].get('integration'),r['value'][1]) for r in Q('alertmanager_notifications_failed_total{integration="webhook"}')], [r['value'][1] for r in Q('alertmanager_notifications_total{integration="webhook"}')])
sh("docker","stop","-t","1",c(n,"sink")); t=time.time(); time.sleep(170)
for e in ['increase(alertmanager_notifications_failed_total{integration="webhook"}[2m])','increase(alertmanager_notifications_total{integration="webhook"}[2m])','rate(alertmanager_notification_latency_seconds_sum{integration="webhook"}[2m])','increase(prometheus_notifications_errors_total{job="prom"}[2m])','up{job="am"}','alertmanager_alerts{state="active"}']:
    print(e,[r['value'][1][:8] for r in Q(e)])
print(sh("docker","logs","--tail","2",c(n,"am"))[:0], sh("bash","-c",f"docker logs {c(n,'am')} 2>&1 | grep -c 'Notify for alerts failed'"))
print(sh("bash","-c",f"docker logs {c(n,'am')} 2>&1 | grep 'Notify for alerts failed' | tail -1 | cut -c1-240"))
lab.down(n)
