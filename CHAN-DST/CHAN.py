import csv
import os
import logging
import argparse
import random
import collections
from tqdm import tqdm, trange
import json

import pdb
from model import BeliefTracker
import numpy as np
import torch
from torch.utils.data import (
    TensorDataset,
    DataLoader,
    RandomSampler,
    SequentialSampler,
    Dataset,
)
from torch.utils.data.distributed import DistributedSampler
from transformers import (
    BertTokenizer,
    AdamW,
    get_linear_schedule_with_warmup,
    get_cosine_with_hard_restarts_schedule_with_warmup,
)


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.device_count() > 0:
        torch.cuda.manual_seed_all(seed)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group["lr"]


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None, update=None):
        """Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label
        self.update = update


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_len, label_id, update, guid):
        self.input_ids = input_ids
        self.input_len = input_len
        self.label_id = label_id
        self.update = update
        self.guid = guid


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        with open(input_file, "r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                if (
                    len(line) > 0 and line[0][0] == "#"
                ):  # ignore comments (starting with '#')
                    continue
                lines.append(line)
            return lines


class Processor(DataProcessor):
    """Processor for the belief tracking dataset (GLUE version)."""

    def __init__(self, config):
        super(Processor, self).__init__()

        self.ontology = json.load(open(os.path.join(config.data_dir, "ontology.json")))
        self.nslots = len(self.ontology.keys())
        self.target_slot = list(self.ontology.keys())
        self.target_slot_idx = [*range(0, self.nslots)]

    def get_train_examples(self, data_dir, accumulation=False):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "train.tsv")), "train", accumulation
        )

    def get_dev_examples(self, data_dir, accumulation=False):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "dev.tsv")), "dev", accumulation
        )

    def get_test_examples(self, data_dir, accumulation=False):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "test.tsv")), "test", accumulation
        )

    def get_labels(self):
        """See base class."""
        return [self.ontology[slot] for slot in self.target_slot]

    def _create_examples(self, lines, set_type, accumulation=False):
        """Creates examples for the training and dev sets."""
        prev_dialogue_index = None
        examples = []
        for (i, line) in enumerate(lines):
            guid = "%s-%s-%s" % (
                set_type,
                line[0],
                line[1],
            )  # line[0]: dialogue index, line[1]: turn index
            if accumulation:
                if prev_dialogue_index is None or prev_dialogue_index != line[0]:
                    text_a = line[2]
                    text_b = line[3]
                    prev_dialogue_index = line[0]
                else:
                    # The symbol '#' will be replaced with '[SEP]' after tokenization.
                    text_a = line[2] + " # " + text_a
                    text_b = line[3] + " # " + text_b
            else:
                text_a = line[2]  # line[2]: user utterance
                text_b = line[3]  # line[3]: system response

            label = [line[4 + idx] for idx in self.target_slot_idx]
            update = [line[4 + self.nslots + idx] for idx in self.target_slot_idx]

            examples.append(
                InputExample(
                    guid=guid, text_a=text_a, text_b=text_b, label=label, update=update
                )
            )
        return examples


class SUMBTDataset(Dataset):
    def __init__(
        self, examples, label_list, tokenizer, max_seq_length=128, max_turn_length=22
    ):
        self.examples = examples
        self.label_list = label_list
        self.tokenizer = tokenizer
        label_map = [
            {label: i for i, label in enumerate(labels)} for labels in label_list
        ]
        slot_dim = len(label_list)

        self.all_features = []
        prev_dialogue_idx = None

        max_turn = 0
        for (ex_index, example) in enumerate(examples):
            if max_turn < int(example.guid.split("-")[-1]):
                max_turn = int(example.guid.split("-")[-1])

        max_turn_length = min(max_turn + 1, max_turn_length)
        features = []
        for (ex_index, example) in enumerate(examples):
            tokens_a = [
                x if x != "#" else "[SEP]" for x in tokenizer.tokenize(example.text_a)
            ]
            tokens_b = None
            if example.text_b:
                tokens_b = [
                    x if x != "#" else "[SEP]"
                    for x in tokenizer.tokenize(example.text_b)
                ]
                # Modifies `tokens_a` and `tokens_b` in place so that the total
                # length is less than the specified length.
                # Account for [CLS], [SEP], [SEP] with "- 3"
                _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
            else:
                # Account for [CLS] and [SEP] with "- 2"
                if len(tokens_a) > max_seq_length - 2:
                    tokens_a = tokens_a[: (max_seq_length - 2)]

            tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
            input_len = [len(tokens), 0]

            if tokens_b:
                tokens += tokens_b + ["[SEP]"]
                input_len[1] = len(tokens_b) + 1

            input_ids = tokenizer.convert_tokens_to_ids(tokens)

            # Zero-pad up to the sequence length.
            # padding = [0] * (max_seq_length - len(input_ids))
            # input_ids += padding
            # assert len(input_ids) == max_seq_length

            label_id = []
            label_info = "label: "
            for i, label in enumerate(example.label):
                label_id.append(label_map[i][label])
                label_info += "%s (id = %d) " % (label, label_map[i][label])

            curr_dialogue_idx = "-".join(example.guid.split("-")[:-1])
            curr_turn_idx = int(example.guid.split("-")[-1])
            guid = "-".join(example.guid.split("-")[1:-1])

            if prev_dialogue_idx is not None and prev_dialogue_idx != curr_dialogue_idx:
                self.all_features.append(features)
                features = []

            if prev_dialogue_idx is None or prev_turn_idx < max_turn_length:
                features.append(
                    InputFeatures(
                        input_ids=input_ids,
                        input_len=input_len,
                        label_id=label_id,
                        update=list(map(int, example.update)),
                        guid=guid,
                    )
                )

            prev_dialogue_idx = curr_dialogue_idx
            prev_turn_idx = curr_turn_idx

        self.all_features.append(features)

    def __len__(self):
        return len(self.all_features)

    def __getitem__(self, index):
        input_ids = [f.input_ids for f in self.all_features[index]]
        max_len = max([len(i) for i in input_ids])
        for i in range(len(input_ids)):
            input_ids[i] = input_ids[i] + [0] * (max_len - len(input_ids[i]))
        input_ids = torch.tensor(input_ids, dtype=torch.long)
        input_len = torch.tensor(
            [f.input_len for f in self.all_features[index]], dtype=torch.long
        )
        label_ids = torch.tensor(
            [f.label_id for f in self.all_features[index]], dtype=torch.long
        )
        update = torch.tensor(
            [f.update for f in self.all_features[index]], dtype=torch.long
        )
        guids = [f.guid for f in self.all_features[index]]
        return input_ids, input_len, label_ids, update, guids


def collate_fn(batch):
    def padding(seq, pad_token):
        max_len = max([i.size(0) for i in seq])
        max_dim = max([i.size(1) for i in seq])
        result = torch.ones((len(seq), max_len, max_dim)).long() * pad_token
        for i in range(len(seq)):
            result[i, : seq[i].size(0), : seq[i].size(1)] = seq[i]
        return result

    (
        input_ids_list,
        input_len_list,
        label_ids_list,
        update_list,
        guids_list,
        num_turns_list,
    ) = ([], [], [], [], [], [])
    for i in batch:
        input_ids_list.append(i[0])
        input_len_list.append(i[1])
        label_ids_list.append(i[2])
        update_list.append(i[3])
        guids_list.append(i[4])
        num_turns_list.append(i[0].size(0))

    input_ids = padding(input_ids_list, torch.LongTensor([0]))
    input_len = padding(input_len_list, torch.LongTensor([0]))
    label_ids = padding(label_ids_list, torch.LongTensor([-1]))
    update = padding(update_list, torch.LongTensor([-1])).float()
    return input_ids, input_len, label_ids, update, guids_list, num_turns_list


def convert_examples_to_features(
    examples, label_list, max_seq_length, tokenizer, max_turn_length
):
    """Loads a data file into a list of `InputBatch`s."""

    label_map = [{label: i for i, label in enumerate(labels)} for labels in label_list]
    slot_dim = len(label_list)

    features = []
    prev_dialogue_idx = None
    all_padding = [0] * max_seq_length
    all_padding_len = [0, 0]

    max_turn = 0
    for (ex_index, example) in enumerate(examples):
        if max_turn < int(example.guid.split("-")[2]):
            max_turn = int(example.guid.split("-")[2])
    max_turn_length = min(max_turn + 1, max_turn_length)
    logger.info("max_turn_length = %d" % max_turn)

    for (ex_index, example) in enumerate(examples):
        tokens_a = [
            x if x != "#" else "[SEP]" for x in tokenizer.tokenize(example.text_a)
        ]
        tokens_b = None
        if example.text_b:
            tokens_b = [
                x if x != "#" else "[SEP]" for x in tokenizer.tokenize(example.text_b)
            ]
            # Modifies `tokens_a` and `tokens_b` in place so that the total
            # length is less than the specified length.
            # Account for [CLS], [SEP], [SEP] with "- 3"
            _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
        else:
            # Account for [CLS] and [SEP] with "- 2"
            if len(tokens_a) > max_seq_length - 2:
                tokens_a = tokens_a[: (max_seq_length - 2)]

        # The convention in BERT is:
        # (a) For sequence pairs:
        #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
        #  type_ids: 0   0  0    0    0     0       0 0    1  1  1  1   1 1
        # (b) For single sequences:
        #  tokens:   [CLS] the dog is hairy . [SEP]
        #  type_ids: 0   0   0   0  0     0 0
        #
        # Where "type_ids" are used to indicate whether this is the first
        # sequence or the second sequence. The embedding vectors for `type=0` and
        # `type=1` were learned during pre-training and are added to the wordpiece
        # embedding vector (and position vector). This is not *strictly* necessary
        # since the [SEP] token unambigiously separates the sequences, but it makes
        # it easier for the model to learn the concept of sequences.
        #
        # For classification tasks, the first vector (corresponding to [CLS]) is
        # used as as the "sentence vector". Note that this only makes sense because
        # the entire model is fine-tuned.

        tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
        input_len = [len(tokens), 0]

        if tokens_b:
            tokens += tokens_b + ["[SEP]"]
            input_len[1] = len(tokens_b) + 1

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # Zero-pad up to the sequence length.
        padding = [0] * (max_seq_length - len(input_ids))
        input_ids += padding
        assert len(input_ids) == max_seq_length

        label_id = []
        label_info = "label: "
        for i, label in enumerate(example.label):
            label_id.append(label_map[i][label])
            label_info += "%s (id = %d) " % (label, label_map[i][label])

        # if ex_index < 5:
        #    logger.info("*** Example ***")
        #    logger.info("guid: %s" % example.guid)
        #    logger.info("tokens: %s" % " ".join(
        #        [str(x) for x in tokens]))
        #    logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
        #    logger.info("input_len: %s" % " ".join([str(x) for x in input_len]))
        #    logger.info("label: " + label_info)

        curr_dialogue_idx = example.guid.split("-")[1]
        curr_turn_idx = int(example.guid.split("-")[2])

        if prev_dialogue_idx is not None and prev_dialogue_idx != curr_dialogue_idx:
            if prev_turn_idx < max_turn_length:
                features += [
                    InputFeatures(
                        input_ids=all_padding,
                        input_len=all_padding_len,
                        label_id=[-1] * slot_dim,
                        update=[-1] * slot_dim,
                    )
                ] * (max_turn_length - prev_turn_idx - 1)
            assert len(features) % max_turn_length == 0

        if prev_dialogue_idx is None or prev_turn_idx < max_turn_length:
            features.append(
                InputFeatures(
                    input_ids=input_ids,
                    input_len=input_len,
                    label_id=label_id,
                    update=list(map(int, example.update)),
                )
            )

        prev_dialogue_idx = curr_dialogue_idx
        prev_turn_idx = curr_turn_idx

    if prev_turn_idx < max_turn_length:
        features += [
            InputFeatures(
                input_ids=all_padding,
                input_len=all_padding_len,
                label_id=[-1] * slot_dim,
                update=[-1] * slot_dim,
            )
        ] * (max_turn_length - prev_turn_idx - 1)
    assert len(features) % max_turn_length == 0

    all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
    all_input_len = torch.tensor([f.input_len for f in features], dtype=torch.long)
    all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.long)
    all_update = torch.tensor([f.update for f in features], dtype=torch.float)

    # reshape tensors to [#batch, #max_turn_length, #max_seq_length]
    all_input_ids = all_input_ids.view(-1, max_turn_length, max_seq_length)
    all_input_len = all_input_len.view(-1, max_turn_length, 2)
    all_label_ids = all_label_ids.view(-1, max_turn_length, slot_dim)
    all_update = all_update.view(-1, max_turn_length, slot_dim)

    return all_input_ids, all_input_len, all_label_ids, all_update


def get_label_embedding(labels, max_seq_length, tokenizer, device):
    features = []
    for label in labels:
        label_tokens = ["[CLS]"] + tokenizer.tokenize(label) + ["[SEP]"]
        label_token_ids = tokenizer.convert_tokens_to_ids(label_tokens)
        label_len = len(label_token_ids)

        label_padding = [0] * (max_seq_length - len(label_token_ids))
        label_token_ids += label_padding
        assert len(label_token_ids) == max_seq_length

        features.append((label_token_ids, label_len))

    all_label_token_ids = torch.tensor([f[0] for f in features], dtype=torch.long).to(
        device
    )
    all_label_len = torch.tensor([f[1] for f in features], dtype=torch.long).to(device)

    return all_label_token_ids, all_label_len


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()


def warmup_linear(x, warmup=0.002):
    if x < warmup:
        return x / warmup
    return 1.0 - x


def main():
    set_seed(42)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data_dir",
        default="./data",
        type=str,
    )
    parser.add_argument(
        "--bert_model",
        default="dsksd/bert-ko-small-minimal",
        type=str,
    )
    parser.add_argument(
        "--target_slot",
        default="all",
        type=str,
    )
    parser.add_argument(
        "--output_dir",
        default="./result",
        type=str,
    )
    parser.add_argument("--do_eval_best_acc", action="store_false")
    parser.add_argument("--model", default="model", type=str, help="model file name")
    parser.add_argument("--mt_drop", type=float, default=0.1)
    parser.add_argument("--window", default=1, type=int)
    parser.add_argument(
        "--max_seq_length",
        default=128,
        type=int,
        help="The maximum total input sequence length after WordPiece tokenization. \n"
        "Sequences longer than this will be truncated, and sequences shorter \n"
        "than this will be padded.",
    )
    parser.add_argument(
        "--max_label_length",
        default=22,
        type=int,
    )
    parser.add_argument(
        "--max_turn_length",
        default=22,
        type=int,
    )
    parser.add_argument(
        "--hidden_dim",
        type=int,
        default=300,
        help="hidden dimension used in belief tracker",
    )
    parser.add_argument(
        "--num_rnn_layers", type=int, default=1, help="number of RNN layers"
    )
    parser.add_argument(
        "--zero_init_rnn", action="store_true", help="set initial hidden of rnns zero"
    )
    parser.add_argument(
        "--attn_head",
        type=int,
        default=6,
    )
    parser.add_argument("--do_train", action="store_false")
    parser.add_argument("--do_eval", action="store_true")
    parser.add_argument(
        "--distance_metric",
        type=str,
        default="euclidean",
    )
    parser.add_argument(
        "--train_batch_size",
        default=4,
        type=int,
    )
    parser.add_argument(
        "--dev_batch_size",
        default=1,
        type=int,
    )
    parser.add_argument(
        "--eval_batch_size",
        default=4,
        type=int,
    )
    parser.add_argument(
        "--num_train_epochs",
        default=50.0,
        type=float,
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
    )
    parser.add_argument("--lamb", default=0.5, type=float)
    parser.add_argument(
        "--learning_rate",
        default=1e-5,
        type=float,
    )
    parser.add_argument(
        "--fix_utterance_encoder",
        action="store_true",
    )
    parser.add_argument(
        "--warmup_proportion",
        default=0.001,
        type=float,
    )
    parser.add_argument(
        "--patience",
        default=20.0,
        type=float,
        help="The number of epochs to allow no further improvement.",
    )

    args = parser.parse_args()
    n_gpu = torch.cuda.device_count()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    processor = Processor(args)
    label_list = processor.get_labels()
    num_labels = [len(labels) for labels in label_list]
    slot_meta = json.load(open("../input/data/train_dataset/slot_meta.json"))
    ontology = json.load(open("../input/data/train_dataset/ontology.json"))
    tokenizer = BertTokenizer.from_pretrained(args.bert_model)
    num_train_steps = None
    accumulation = False
    if args.do_train:
        train_examples = processor.get_train_examples(
            args.data_dir, accumulation=accumulation
        )

        dev_examples = processor.get_dev_examples(
            args.data_dir, accumulation=accumulation
        )
        train_dataset = SUMBTDataset(
            train_examples,
            label_list,
            tokenizer,
            max_seq_length=args.max_seq_length,
            max_turn_length=args.max_turn_length,
        )

        num_train_batches = len(train_dataset)
        num_train_steps = int(
            num_train_batches / args.train_batch_size * args.num_train_epochs
        )
        train_sampler = RandomSampler(train_dataset)
        train_dataloader = DataLoader(
            train_dataset,
            sampler=train_sampler,
            batch_size=args.train_batch_size,
            num_workers=4,
            collate_fn=lambda x: collate_fn(x),
        )
        dev_dataset = SUMBTDataset(
            dev_examples,
            label_list,
            tokenizer,
            max_seq_length=args.max_seq_length,
            max_turn_length=args.max_turn_length,
        )
        dev_sampler = SequentialSampler(dev_dataset)
        dev_dataloader = DataLoader(
            dev_dataset,
            sampler=dev_sampler,
            batch_size=args.dev_batch_size,
            num_workers=4,
            collate_fn=lambda x: collate_fn(x),
        )
        ############################################################
        # TEST#
        ############################################################
        test_examples = processor.get_test_examples(
            args.data_dir, accumulation=accumulation
        )
        test_dataset = SUMBTDataset(
            test_examples,
            label_list,
            tokenizer,
            max_seq_length=args.max_seq_length,
            max_turn_length=args.max_turn_length,
        )
        test_sampler = SequentialSampler(test_dataset)
        test_dataloader = DataLoader(
            test_dataset,
            sampler=test_sampler,
            batch_size=4,
            num_workers=4,
            collate_fn=lambda x: collate_fn(x),
        )
    ###############################################################################
    # Build the models
    ###############################################################################
    model = BeliefTracker(args, num_labels, device)

    model.to(device)

    ## Get slot-value embeddings
    label_token_ids, label_len = [], []
    for labels in label_list:
        token_ids, lens = get_label_embedding(
            labels, args.max_label_length, tokenizer, device
        )
        label_token_ids.append(token_ids)
        label_len.append(lens)

    ## Get domain-slot-type embeddings
    slot_token_ids, slot_len = get_label_embedding(
        processor.target_slot, args.max_label_length, tokenizer, device
    )

    ## Initialize slot and value embeddings
    model.initialize_slot_value_lookup(label_token_ids, slot_token_ids)

    output_model_file = os.path.join(
        args.output_dir, "acc_more_and_more_and_more_and_more.best"
    )
    ptr_model = torch.load(output_model_file)
    state = model.state_dict()
    state.update(ptr_model)
    model.load_state_dict(state)

    if args.do_train:

        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p
                    for n, p in model.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.01,
            },
            {
                "params": [
                    p
                    for n, p in model.named_parameters()
                    if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]

        t_total = len(train_dataloader) * args.num_train_epochs

        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=1e-8)
        scheduler = get_linear_schedule_with_warmup(
            optimizer,
            num_warmup_steps=int(t_total * args.warmup_proportion),
            num_training_steps=t_total,
        )
        # scheduler = get_cosine_with_hard_restarts_schedule_with_warmup(
        #     optimizer,
        #     num_warmup_steps=int(t_total * args.warmup_proportion),
        #     num_training_steps=t_total,
        #     num_cycles=args.num_train_epochs // 10,
        # )
    ###############################################################################
    # Training code
    ###############################################################################
    if not args.do_train:
        global_step = 0
        last_update = None
        best_loss = None
        best_acc = None
        train_mt = True

        for epoch in range(int(args.num_train_epochs)):
            model.train()
            tr_loss = 0
            total_loss = 0
            nb_tr_examples = 0
            nb_tr_steps = 0

            for step, batch in enumerate(tqdm(train_dataloader)):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_len, label_ids, update = batch

                loss, loss_slot, acc, acc_slot, _, tup = model(
                    input_ids, input_len, label_ids, update, n_gpu, mt=train_mt
                )

                loss.backward()

                tr_loss += loss.item()
                total_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    # modify lealrning rate with special warm up BERT uses
                    # lr_this_step = args.learning_rate * warmup_linear(
                    #     global_step / t_total, args.warmup_proportion
                    # )
                    # if summary_writer is not None:
                    #    summary_writer.add_scalar("Train/LearningRate", lr_this_step, global_step)
                    # for param_group in optimizer.param_groups:
                    #     param_group["lr"] = lr_this_step
                    optimizer.step()
                    scheduler.step()
                    optimizer.zero_grad()
                    global_step += 1
                if (step + 1) % 100 == 0:
                    print(
                        f"epoch:{epoch} [{step}/{len(train_dataloader)}] loss:{total_loss} acc:{acc} lr:{get_lr(optimizer)}"
                    )
                    total_loss = 0
            badcase_list = []
            model.eval()
            dev_loss = 0
            dev_acc = 0
            dev_loss_slot, dev_acc_slot = None, None
            dev_tup = [0, 0, 0]
            nb_dev_examples, nb_dev_steps = 0, 0

            for step, batch in enumerate(dev_dataloader):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_len, label_ids, update = batch
                if input_ids.dim() == 2:
                    input_ids = input_ids.unsqueeze(0)
                    input_len = input_len.unsqueeze(0)
                    label_ids = label_ids.unsuqeeze(0)
                    update = update.unsqueeze(0)

                with torch.no_grad():
                    loss, loss_slot, acc, acc_slot, pred_slot, tup = model(
                        input_ids, input_len, label_ids, update, n_gpu
                    )

                badcase = (pred_slot != label_ids) * (label_ids > -1)
                badcase_1 = badcase.sum(-1).nonzero()
                for b in badcase_1:
                    b_idx = b.cpu().numpy().tolist()
                    sent = " ".join(
                        tokenizer.convert_ids_to_tokens(
                            input_ids[b_idx[0], b_idx[1]].cpu().numpy().tolist()
                        )
                    )
                    pred = " ".join(
                        [
                            label_list[i][j]
                            for i, j in enumerate(
                                pred_slot[b_idx[0], b_idx[1]].cpu().numpy().tolist()
                            )
                        ]
                    )
                    gold = " ".join(
                        [
                            label_list[i][j]
                            for i, j in enumerate(
                                label_ids[b_idx[0], b_idx[1]].cpu().numpy().tolist()
                            )
                        ]
                    )
                    badcase_list.append(f"{sent}\t{pred}\t{gold}\n")
                num_valid_turn = torch.sum(label_ids[:, :, 0].view(-1) > -1, 0).item()
                dev_loss += loss.item() * num_valid_turn
                dev_acc += acc.item() * num_valid_turn
                dev_tup[0] += tup[0] * num_valid_turn
                dev_tup[1] += tup[1] * num_valid_turn
                dev_tup[2] += tup[2] * num_valid_turn
                if dev_loss_slot is None:
                    dev_loss_slot = [l * num_valid_turn for l in loss_slot]
                    dev_acc_slot = acc_slot * num_valid_turn
                else:
                    for i, l in enumerate(loss_slot):
                        dev_loss_slot[i] = dev_loss_slot[i] + l * num_valid_turn
                    dev_acc_slot += acc_slot * num_valid_turn

                nb_dev_examples += num_valid_turn

            dev_loss = dev_loss / nb_dev_examples
            dev_acc = dev_acc / nb_dev_examples
            dev_tup = list(map(lambda x: x / nb_dev_examples, dev_tup))
            # train_mt = dev_tup[2] < 0.95

            dev_acc_slot = dev_acc_slot / nb_dev_examples
            print(f"dev_acc:{dev_acc} dev_acc_slot:{dev_acc_slot}")
            dev_loss = round(dev_loss, 6)
            if last_update is None or dev_acc > best_acc:
                # Save a trained model
                output_model_file = os.path.join(
                    args.output_dir, "acc_more_and_more_and_more_and_more.best"
                )
                if args.do_train:
                    torch.save(model.state_dict(), output_model_file)
                best_acc = dev_acc
            if last_update is None or dev_tup[0] < best_loss:
                badcase_fp = open(
                    os.path.join(
                        args.output_dir,
                        "dev_badcase_more_and_more_and_more_and_more.txt",
                    ),
                    "w",
                    encoding="utf-8",
                )
                for line in badcase_list:
                    badcase_fp.write(line)
                badcase_fp.close()
                # Save a trained model
                output_model_file = os.path.join(
                    args.output_dir, "loss_more_and_more_and_more_and_more.best"
                )
                if args.do_train:
                    torch.save(model.state_dict(), output_model_file)
                last_update = epoch
                best_loss = dev_tup[0]
            if last_update + args.patience <= epoch:
                break
    print("test_start")
    model.eval()
    predictions = {}
    for batch in tqdm(test_dataloader):
        input_ids, input_len, label_ids, update, guids, num_turns = [
            b.to(device) if not isinstance(b, list) else b for b in batch
        ]
        if input_ids.dim() == 2:
            input_ids = input_ids.unsqueeze(0)
            input_len = input_len.unsqueeze(0)
            label_ids = label_ids.unsuqeeze(0)
        with torch.no_grad():
            if n_gpu == 1:
                _, pred_slot = model(
                    input_ids, input_len, labels=None, update=None, n_gpu=n_gpu
                )
        batch_size = input_ids.size(0)
        for i in range(batch_size):
            states = []
            guid = guids[i][0]
            pred_slots = pred_slot.tolist()[i]
            num_turn = num_turns[i]
            for pred in pred_slots[:num_turn]:
                state = []
                for s, p in zip(slot_meta, pred):
                    v = ontology[s][p]
                    if v != "none":
                        state.append(f"{s}-{v}")
                states.append(state)
            for tid, state in enumerate(states):
                predictions[f"{guid}-{tid}"] = state
    json.dump(
        predictions, open("predictions_CHAN.csv", "w"), indent=2, ensure_ascii=False
    )

    ###############################################################################
    # Evaluation
    ###############################################################################
    if args.do_eval_best_acc:
        output_model_file = os.path.join(args.output_dir, "acc.best")
    else:
        output_model_file = os.path.join(args.output_dir, "loss.best")
    model = BeliefTracker(args, num_labels, device)
    ptr_model = torch.load(output_model_file)

    state = model.state_dict()
    state.update(ptr_model)
    model.load_state_dict(state)
    model.to(device)
    eval_examples = processor.get_test_examples(
        args.data_dir, accumulation=accumulation
    )

    # eval_data = TensorDataset(all_input_ids, all_input_len, all_label_ids, all_update)
    eval_dataset = SUMBTDataset(
        eval_examples,
        label_list,
        tokenizer,
        max_seq_length=args.max_seq_length,
        max_turn_length=args.max_turn_length,
    )

    # Run prediction for full data
    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(
        eval_dataset,
        sampler=eval_sampler,
        batch_size=args.eval_batch_size,
        collate_fn=lambda x: collate_fn(x),
    )

    model.eval()
    eval_loss, eval_accuracy = 0, 0
    eval_update_acc = 0
    eval_loss_slot, eval_acc_slot = None, None
    nb_eval_steps, nb_eval_examples = 0, 0

    accuracies = {
        "joint7": 0,
        "slot7": 0,
        "joint5": 0,
        "slot5": 0,
        "joint_rest": 0,
        "slot_rest": 0,
        "num_turn": 0,
        "num_slot7": 0,
        "num_slot5": 0,
        "num_slot_rest": 0,
    }

    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        batch = tuple(t.to(device) for t in batch)
        input_ids, input_len, label_ids, update = batch
        if input_ids.dim() == 2:
            input_ids = input_ids.unsqueeze(0)
            input_len = input_len.unsqueeze(0)
            label_ids = label_ids.unsuqeeze(0)
            update = udpate.unsqueeze(0)

        with torch.no_grad():
            if n_gpu == 1:
                _, pred_slot = model(
                    input_ids, input_len, labels=None, update=None, n_gpu=n_gpu
                )
                # loss, loss_slot, acc, acc_slot, pred_slot, tup = model(
                #     input_ids, input_len, label_ids, update, n_gpu
                # )
            else:
                loss, _, acc, acc_slot, pred_slot, tup_1, tup_2, tup_3 = model(
                    input_ids, input_len, label_ids, update, n_gpu
                )
                tup = (tup_1.mean(), tup_2.mean(), tup_3.mean())
                nbatch = label_ids.size(0)
                nslot = pred_slot.size(3)
                pred_slot = pred_slot.view(nbatch, -1, nslot)

        accuracies = eval_all_accs(pred_slot, label_ids, accuracies)

        nb_eval_ex = (label_ids[:, :, 0].view(-1) != -1).sum().item()
        nb_eval_examples += nb_eval_ex
        nb_eval_steps += 1
        eval_update_acc += tup[2] * nb_eval_ex

        if n_gpu == 1:
            eval_loss += loss.item() * nb_eval_ex
            eval_accuracy += acc.item() * nb_eval_ex
            if eval_loss_slot is None:
                eval_loss_slot = [l * nb_eval_ex for l in loss_slot]
                eval_acc_slot = acc_slot * nb_eval_ex
            else:
                for i, l in enumerate(loss_slot):
                    eval_loss_slot[i] = eval_loss_slot[i] + l * nb_eval_ex
                eval_acc_slot += acc_slot * nb_eval_ex
        else:
            eval_loss += sum(loss) * nb_eval_ex
            eval_accuracy += sum(acc) * nb_eval_ex

    eval_update_acc = eval_update_acc / nb_eval_examples
    eval_loss = eval_loss / nb_eval_examples
    eval_accuracy = eval_accuracy / nb_eval_examples
    if n_gpu == 1:
        eval_acc_slot = eval_acc_slot / nb_eval_examples

    loss = tr_loss / nb_tr_steps if args.do_train else None

    if n_gpu == 1:
        result = {
            "eval_loss": eval_loss,
            "eval_accuracy": eval_accuracy,
            "loss": loss,
            "eval_loss_slot": "\t".join(
                [str(val / nb_eval_examples) for val in eval_loss_slot]
            ),
            "eval_acc_slot": "\t".join([str((val).item()) for val in eval_acc_slot]),
        }
    else:
        result = {
            "eval_loss": eval_loss,
            "eval_accuracy": eval_accuracy,
            "loss": loss,
        }

    out_file_name = "eval_results"
    if args.target_slot == "all":
        out_file_name += "_all"
    output_eval_file = os.path.join(args.output_dir, "%s.txt" % out_file_name)

    if n_gpu == 1:
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))

    out_file_name = "eval_all_accuracies"
    with open(os.path.join(args.output_dir, "%s.txt" % out_file_name), "w") as f:
        f.write(
            "joint acc (7 domain) : slot acc (7 domain) : joint acc (5 domain): slot acc (5 domain): joint restaurant : slot acc restaurant \n"
        )
        f.write(
            "%.5f : %.5f : %.5f : %.5f : %.5f : %.5f \n"
            % (
                (accuracies["joint7"] / accuracies["num_turn"]).item(),
                (accuracies["slot7"] / accuracies["num_slot7"]).item(),
                (accuracies["joint5"] / accuracies["num_turn"]).item(),
                (accuracies["slot5"] / accuracies["num_slot5"]).item(),
                (accuracies["joint_rest"] / accuracies["num_turn"]).item(),
                (accuracies["slot_rest"] / accuracies["num_slot_rest"]).item(),
            )
        )


if __name__ == "__main__":
    main()
