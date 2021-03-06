import pytorch_lightning as pl
import torch
import transformers
from dataset import TranslationDataset, TranslationLazyDataset


class PadFunction(object):
    def __init__(self, pad_id=0):
        self.pad_id = pad_id

    def __call__(self, batch):
        return self._pad_fn(batch)

    # @profile
    def merge(self, sequences, pad_size=None):
        lengths = [len(seq) for seq in sequences]
        if pad_size is None:
            pad_size = max(lengths)
        padded_seqs = torch.full(
            (len(lengths), pad_size), self.pad_id, dtype=torch.long)
        for i, seq in enumerate(sequences):
            end = lengths[i]
            padded_seqs[i, :end] = seq[:end]
        return padded_seqs, lengths

    # @profile
    def make_mask(self, inputs, inputs_length):
        max_len = inputs.shape[1]
        inputs_mask = torch.arange(max_len).expand(len(inputs_length), max_len) < torch.tensor(inputs_length).unsqueeze(1)
        return inputs_mask

    # @profile
    def _pad_fn(self, batch):
        # sort a list by sequence length (descending order) to use pack_padded_sequence
        # batch.sort(key=lambda x: len(x[0]), reverse=True)

        # seperate source and target sequences
        src_seqs, trg_seqs = tuple(zip(*batch))

        # merge sequences (from tuple of 1D tensor to 2D tensor)
        # pad_size = max([len(seq) for seq in src_seqs] + [len(seq) for seq in trg_seqs])
        pad_size = None
        src_seqs, src_lengths = self.merge(src_seqs, pad_size)
        trg_seqs, trg_lengths = self.merge(trg_seqs, pad_size)

        source_tokens = {
            'token_ids': src_seqs,
            'mask': self.make_mask(src_seqs, src_lengths)
        }

        target_tokens = {
            'token_ids': trg_seqs,
            'mask': self.make_mask(trg_seqs, trg_lengths)
        }
        return source_tokens, target_tokens


class BartForMaskedLM(pl.LightningModule):
    def __init__(self):
        super().__init__()

        self.batch_size = 8
        self.learning_rate = 3e-5
        self.d_model = 1024

        self.tokenizer = transformers.BertTokenizer('./vocab/vocab.txt', do_basic_tokenize=False)
        setattr(self.tokenizer, "_bos_token", '[CLS]')
        setattr(self.tokenizer, "_eos_token", '[SEP]')

        self.bos_token_id = self.tokenizer.bos_token_id
        self.eos_token_id = self.tokenizer.eos_token_id
        self.pad_token_id = self.tokenizer.pad_token_id

        self.vocab_size = self.tokenizer.vocab_size

        self.config = transformers.BartConfig(
            vocab_size=self.vocab_size,
            d_model=self.d_model,
            encoder_layers=6,
            decoder_layers=6,
            max_position_embeddings=512,
            bos_token_id=self.bos_token_id,
            eos_token_id=self.eos_token_id,
            pad_token_id=self.pad_token_id,
            use_cache=False,
        )
        self.transformer = transformers.BartModel(self.config)
        self.lm_head = torch.nn.Linear(self.d_model, self.vocab_size, bias=False)

    # @profile
    def forward(self, source_tokens, target_tokens):
        inputs, labels = source_tokens, target_tokens

        input_ids, input_mask = inputs["token_ids"], inputs["mask"]
        label_ids, label_mask = labels["token_ids"], labels["mask"]

        batch_size = input_ids.shape[0]

        # in lightning, forward defines the prediction/inference actions
        transformer_outputs = self.transformer(
            input_ids=input_ids,
            attention_mask=input_mask,
            decoder_input_ids=label_ids,
            decoder_attention_mask=label_mask,
            use_cache=False,
        )
        # (batch_size, sequence_length, hidden_size)
        hidden_states = transformer_outputs.last_hidden_state
        # (batch_size, sequence_length, vocab_size)
        lm_logits = self.lm_head(hidden_states)

        return lm_logits

    # @profile
    def training_step(self, batch, batch_idx):
        # training_step defined the train loop.
        # It is independent of forward
        inputs, labels = batch

        input_ids, input_mask = inputs["token_ids"], inputs["mask"]
        label_ids, label_mask = labels["token_ids"], labels["mask"]

        batch_size = input_ids.shape[0]

        lm_logits = self.forward(
            source_tokens={
                'token_ids': input_ids,
                'mask': input_mask
            },
            target_tokens={
                'token_ids': label_ids[..., :-1],
                'mask': label_mask[..., :-1]
            }
        )

        shift_label_ids = label_ids[..., 1:].contiguous()

        loss_fct = torch.nn.CrossEntropyLoss(ignore_index=self.pad_token_id)
        loss = loss_fct(lm_logits.view(-1, self.vocab_size),
                        shift_label_ids.view(-1))

        # Logging to TensorBoard by default
        self.log('train_loss', loss)
        return loss

    def validation_step(self, batch, batch_idx):
        inputs, labels = batch

        input_ids, input_mask = inputs["token_ids"], inputs["mask"]
        label_ids, label_mask = labels["token_ids"], labels["mask"]

        batch_size = input_ids.shape[0]

        lm_logits = self.forward(
            source_tokens={
                'token_ids': input_ids,
                'mask': input_mask
            },
            target_tokens={
                'token_ids': label_ids[..., :-1],
                'mask': label_mask[..., :-1]
            }
        )

        shift_label_ids = label_ids[..., 1:].contiguous()

        loss_fct = torch.nn.CrossEntropyLoss(ignore_index=self.pad_token_id)
        loss = loss_fct(lm_logits.view(-1, self.vocab_size),
                        shift_label_ids.view(-1))
        self.log('val_loss', loss)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        return optimizer

    def train_dataloader(self):
        ai_challenger_2017_dataset = TranslationLazyDataset(
            'data/ai_challenger_2017_train.en', 'data/ai_challenger_2017_train.zh', tokenizer=self.tokenizer)
        minecraft_dataset = TranslationLazyDataset(
            'data/minecraft.en', 'data/minecraft.zh', tokenizer=self.tokenizer)
        translation2019zh_dataset = TranslationLazyDataset(
            'data/translation2019zh_train.en', 'data/translation2019zh_train.zh', tokenizer=self.tokenizer)
        MultiUN_en_zh_dataset = TranslationLazyDataset(
            'data/MultiUN.en-zh.en', 'data/MultiUN.en-zh.zh', tokenizer=self.tokenizer)
        umcorpus_dataset = TranslationLazyDataset(
            'data/umcorpus.en', 'data/umcorpus.zh', tokenizer=self.tokenizer)
        news_commentary_dataset = TranslationLazyDataset(
            'data/news-commentary-v12.zh-en.en', 'data/news-commentary-v12.zh-en.zh', tokenizer=self.tokenizer)
        ted_dataset = TranslationLazyDataset(
            'data/ted_train_en-zh.raw.en', 'data/ted_train_en-zh.raw.zh', tokenizer=self.tokenizer)

        dataset = torch.utils.data.ConcatDataset(
            [
                ai_challenger_2017_dataset,
                minecraft_dataset,
                translation2019zh_dataset,
                MultiUN_en_zh_dataset,
                umcorpus_dataset,
                news_commentary_dataset,
                ted_dataset,
            ]
        )
        train_sampler = torch.utils.data.RandomSampler(
            dataset, num_samples=len(dataset)//100, replacement=True)

        pad_fn_object = PadFunction(self.tokenizer.pad_token_id)
        train_loader = torch.utils.data.DataLoader(
            dataset, num_workers=8, batch_size=self.batch_size, collate_fn=pad_fn_object, sampler=train_sampler, pin_memory=True)

        return train_loader

    def val_dataloader(self):
        translation2019zh_valid_dataset = TranslationLazyDataset(
            'data/translation2019zh_valid.en', 'data/translation2019zh_valid.zh', tokenizer=self.tokenizer)

        valid_dataset = translation2019zh_valid_dataset

        valid_sampler = torch.utils.data.SequentialSampler(valid_dataset)

        pad_fn_object = PadFunction(self.tokenizer.pad_token_id)
        valid_loader = torch.utils.data.DataLoader(
            valid_dataset, num_workers=4, batch_size=self.batch_size, collate_fn=pad_fn_object, sampler=valid_sampler, pin_memory=True)

        return valid_loader

    def test_dataloader(self):
        pass
