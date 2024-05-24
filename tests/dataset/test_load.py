from reallm.api.core import data_api, system_api
import reallm.api.core.system_api
import reallm.impl.dataset
import reallm.impl.model

if __name__ == "__main__":
    dataset_config = system_api.Dataset(
        "wpsf_plrw_packed",
        args=dict(
            contrastive_dim=6,
            enforce_one_or_less_pos=False,
            n_tokens_per_batch=5120,
            max_n_seqs_per_batch=100,
            max_length=1024,
            json_path="/data/aigc/llm/datasets/wps-formula-rw/dataset_val.jsonl",
        ))
    dataloader_cfg = system_api.DataLoader('iterable_dataset_loader')
    dataset = data_api.make_dataset(dataset_config,
                                    seed=1,
                                    ddp_rank=0,
                                    world_size=1,
                                    tokenizer_or_tokenizer_name="/data/aigc/llm/checkpoints/4l-starcoder/",
                                    experiment_name="dataset_test",
                                    trial_name='test')
    dataloder = data_api.make_dataloader(dataloader_cfg, dataset)

    for x in dataloder:
        assert isinstance(x, dict)