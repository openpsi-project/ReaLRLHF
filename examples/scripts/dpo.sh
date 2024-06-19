SFT_MODEL_PATH=/lustre/aigc/llm/checkpoints/fw/quickstart-sft/release/default/epoch7epochstep5globalstep50/
CLUSTER_SPEC_PATH=/lustre/aigc/llm/cluster/qh.json python3 -m realhf.apps.quickstart dpo \
    experiment_name=quickstart-dpo trial_name=release \
    n_nodes=1 \
    allocation_mode=manual \
    total_train_epochs=2 \
    save_freq_steps=5 \
    actor.type._class=llama \
    actor.type.size=13 \
    actor.type.is_critic=False \
    actor.path=$SFT_MODEL_PATH \
    actor_train.parallel.pipeline_parallel_size=1 \
    actor_train.parallel.model_parallel_size=4 \
    actor_train.parallel.data_parallel_size=2 \
    actor_train.parallel.use_sequence_parallel=True \
    ref.type._class=llama \
    ref.type.size=13 \
    ref.type.is_critic=False \
    ref.path=$SFT_MODEL_PATH \
    ref_inf.parallel.pipeline_parallel_size=1 \
    ref_inf.parallel.model_parallel_size=2 \
    ref_inf.parallel.data_parallel_size=4 \
    ref_inf.parallel.use_sequence_parallel=True \
    dataset.train_path=/lustre/fw/datasets/imdb/rl/rm_paired-train.jsonl \
    dataset.max_pairs_per_prompt=2 \
    dataset.max_seqlen=256 \
    dataset.train_bs_n_seqs=256 \
    dataset.valid_bs_n_seqs=256