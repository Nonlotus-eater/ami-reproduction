"""One explicitly authorized synthetic LLM metadata probe; no Add/Search or retries."""
import argparse
import json
import logging
import os
from pathlib import Path
import re

import httpx

from .serve import ROOT, read_profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute-authorized-once', action='store_true')
    args = parser.parse_args()
    if not args.execute_authorized_once:
        parser.error('Explicit one-shot flag required')
    logging.disable(logging.CRITICAL)
    profile = read_profile(ROOT / '.env')
    if (profile.get('AMI_LLM_MODEL') != 'gpt-4o-mini' or
            profile.get('OPENAI_BASE_URL') != 'https://linkapi.ai/v1' or not profile.get('OPENAI_API_KEY')):
        raise SystemExit('Configuration mismatch; no API request made')
    output = ROOT / 'artifacts' / 'model-diagnostic-once'
    output.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(output / 'attempt.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        raise SystemExit('Diagnostic already attempted; refuse automatic rerun')
    report = {'llm_attempts': 0, 'embedding_attempts': 0, 'add_attempts': 0, 'search_attempts': 0,
              'requested_model': 'gpt-4o-mini', 'http_status': None, 'result': 'pending'}
    target = output / 'report.json'
    def save():
        target.write_text(json.dumps(report, indent=2) + '\n')
    save()
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            report['llm_attempts'] = 1
            save()
            response = client.post('https://linkapi.ai/v1/chat/completions',
                headers={'Authorization': 'Bearer ' + profile['OPENAI_API_KEY']},
                json={'model':'gpt-4o-mini','messages':[{'role':'user','content':'Reply with OK.'}],
                      'max_tokens':8,'temperature':0,'stream':False})
        report['http_status'] = response.status_code
        if response.status_code != 200:
            report['result'] = 'http_error_stop'
        else:
            body = response.json()
            model = body.get('model')
            report['model_field_present'] = 'model' in body
            report['exact_model_match'] = model == 'gpt-4o-mini'
            # Only an ordinary model identifier may be retained. Never keep provider bodies.
            sensitive = [profile.get(k) for k in ['OPENAI_API_KEY','AMI_EMBED_API_KEY','AMI_AUTH_TOKEN'] if profile.get(k)]
            if (isinstance(model,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}',model)
                    and not model.startswith('sk-') and not any(s in model for s in sensitive)):
                report['returned_model'] = model
            else:
                report['returned_model'] = None
            usage = body.get('usage', {})
            if isinstance(usage,dict):
                report['usage'] = {k:usage[k] for k in ['prompt_tokens','completion_tokens','total_tokens']
                                   if type(usage.get(k)) is int and usage[k]>=0}
            report['result'] = 'metadata_collected_stop' if not body.get('error') else 'provider_error_stop'
    except httpx.RequestError:
        report['result'] = 'network_error_stop'
    except Exception:
        report['result'] = 'invalid_response_stop'
    save()
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
