# python evaluation/run_evaluation.py \
#   --dataset_name "output/minimax/python-attrs/results/results.json" \
#   --predictions_path "gold" \
#   --max_workers 5 \
#   --run_id "python-attrs" \
#   --output_path "run_instances" \
#   --timeout 3600 \
#   --is_judge_fail2pass


python scripts/judge_fail2pass.py run_instances/python-attrs/gold run_instances/python-attrs/gold/fail2pass_status.json