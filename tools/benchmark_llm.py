"""Real API benchmark with synthetic content only. Key from environment."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, llm, parser

RAW = '今天实验没有失败，只是还没完成。我跑了三公里，后来觉得累就停了。上周打算读两篇论文，这周只读完了一篇。我可能需要缩小计划，但还没决定。明天先整理数据，不确定晚上有没有时间继续读。'


def main():
    if not config.ZHIPU_API_KEY:
        raise SystemExit('Set ZHIPU_API_KEY in this process environment first.')
    import argparse
    args_parser = argparse.ArgumentParser()
    args_parser.add_argument('--long', action='store_true')
    args = args_parser.parse_args()
    raw = RAW
    if args.long:
        raw += ''.join('第%d组是虚构实验记录：我检查了样本编号和仪器参数，发现有一个字段还没核对；这不代表实验失败，也不能确定结果。先把原始数据存好，下一次再复核。' % i for i in range(1, 13))
    results = []
    for mode, model in [('', 'glm-5.3-flash'), ('/深度复盘', 'glm-5.3')]:
        config.GLM_MODEL = model
        config.GLM_MODEL_DEEP = model
        related = [{'date':'2026-10-01','source_type':'entry','source_id':i,'content':'虚构历史：计划整理数据、核对参数，结果尚未确认。'} for i in range(1,9)] if args.long else []
        prompt = llm.build_user_prompt({}, [], [], related, raw, '2026-10-19', mode)
        metrics = {}
        start = time.monotonic()
        row = {'mode': mode or 'daily', 'model': model, 'synthetic': True, 'raw_chars': len(raw), 'related_count':len(related)}
        def preview(text):
            if text: metrics.setdefault('first_preview_seconds', round(time.monotonic()-start,3))
        try:
            text = llm.call_glm(prompt, deep=bool(mode), mode=mode, on_preview=preview, metrics=metrics)
            parsed = parser.parse_json_result(text, raw, '2026-10-19', mode)
            row.update(success=True, metrics=metrics, result=parsed)
        except Exception as exc:
            row.update(success=False, error_type=type(exc).__name__, error=str(exc))
        row['seconds'] = round(time.monotonic()-start, 2)
        results.append(row)
        print(json.dumps({k:v for k,v in row.items() if k != 'result'}, ensure_ascii=False), flush=True)
        Path('benchmark-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
