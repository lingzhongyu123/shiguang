"""A successful API trigger is not a successful deployment."""
import argparse
import json
import os
import time
import urllib.request

BASE = 'https://api.render.com/v1/services/srv-db5gq5t9fdbs73cr46bg'


def request(url, token=None, body=None):
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    token = os.environ.get('RENDER_API_KEY')
    if not token:
        raise SystemExit('RENDER_API_KEY is missing: deployment was NOT triggered.')
    # Never repeat POST after ambiguous timeout: it may already have created a deployment.
    deploy = request(BASE + '/deploys', token, {'clearCache': 'do_not_clear', 'commitId': args.commit})
    deploy_id = deploy['id']
    print('Deployment created:', deploy_id, flush=True)
    for _ in range(80):
        time.sleep(15)
        try:
            result = request(BASE + '/deploys/' + deploy_id, token)
        except Exception as exc:
            print('Status temporarily unavailable:', type(exc).__name__, flush=True)
            continue
        status = result['status']
        print('Render status:', status, flush=True)
        if status in ('build_failed', 'update_failed', 'pre_deploy_failed', 'canceled'):
            raise SystemExit('Render deployment failed: ' + status)
        if status == 'live':
            try:
                health = request('https://shiguang-ryos.onrender.com/api/health?version_check=' + str(time.time_ns()))
                if health.get('ok') and health.get('version') == args.commit:
                    print('Verified live commit:', args.commit)
                    return
            except Exception:
                pass
    raise SystemExit('Timed out waiting for the exact commit to become healthy.')


if __name__ == '__main__':
    main()
