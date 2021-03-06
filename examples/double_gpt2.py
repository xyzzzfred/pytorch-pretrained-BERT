# coding=utf-8
# Copyright 2018 Google AI, Google Brain and Carnegie Mellon University Authors and the HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" OpenAI GPT model fine-tuning script.
    Adapted from https://github.com/huggingface/pytorch-openai-transformer-lm/blob/master/train.py
    It self adapted from https://github.com/openai/finetune-transformer-lm/blob/master/train.py

    This script with default values fine-tunes and evaluate a pretrained OpenAI GPT on the RocStories dataset:
        python run_openai_gpt.py \
          --model_name openai-gpt \
          --do_train \
          --do_eval \
          --train_dataset $ROC_STORIES_DIR/cloze_test_val__spring2016\ -\ cloze_test_ALL_val.csv \
          --eval_dataset $ROC_STORIES_DIR/cloze_test_test__spring2016\ -\ cloze_test_ALL_test.csv \
          --output_dir ../log \
          --train_batch_size 16 \
"""
import argparse
import os
import random
import logging
from tqdm import tqdm, trange
from io import open
import datetime
from scipy.misc import logsumexp

import flair

import numpy as np
import torch
import torch.nn as nn
from torch.nn import CrossEntropyLoss

from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)

from pytorch_transformers import (GPT2Config, GPT2LMHeadModel, GPT2Model, AdamW, cached_path, WEIGHTS_NAME, CONFIG_NAME)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S', filename='gpt_2_2.log',
                    level=logging.DEBUG)
logger = logging.getLogger(__name__)

class Dictionary(object):
    def __init__(self):
        self.word2idx = {}
        self.idx2word = []

    def add_word(self, word):
        if word not in self.word2idx:
            self.idx2word.append(word)
            self.word2idx[word] = len(self.idx2word) - 1
        return self.word2idx[word]

    def __len__(self):
        return len(self.idx2word)

class TaggedCorpus(object):
    def __init__(self, path):

        self.dictionary = Dictionary()
        self.pos_dictionary = Dictionary()
        self.train = self.tokenize(os.path.join(path, 'tagged_train.txt'))
        self.valid = self.tokenize(os.path.join(path, 'tagged_valid.txt'))
        self.test = self.tokenize(os.path.join(path, 'tagged_test.txt'))

    def tokenize(self, path):
        """Tokenizes a text file into """
        assert os.path.exists(path)
        # Add words to the dictionary
        with open(path, 'r', encoding="utf8") as f:
            tokens = 0
            for line in f:
                if len(line.strip().split()[::2]) > 3:
                    words = ['<sos>'] + line.strip().split()[::2] + ['<eos>']
                    pos_tags = ['<SOS>'] + line.strip().split()[1::2] + ['<EOS>']
                    tokens += len(words)
                    for word in words:
                        self.dictionary.add_word(word)
                    for tag in pos_tags:
                        self.pos_dictionary.add_word(tag)
        # Tokenize file content
        with open(path, 'r', encoding="utf8") as f:
            ids = torch.LongTensor(tokens)
            pos_ids = torch.LongTensor(tokens)
            token = 0
            pos_token = 0
            for line in f:
                if len(line.strip().split()[::2]) > 3:
                #print (line.strip().split())
                    words = ['<sos>']+line.strip().split()[::2] + ['<eos>']
                    pos_tags = ['<SOS>'] + line.strip().split()[1::2] + ['<EOS>']
                    for word in words:
                        ids[token] = self.dictionary.word2idx[word]
                        token += 1
                    for i,tag in enumerate(pos_tags):
                        pos_ids[pos_token] =self.pos_dictionary.word2idx[tag]
                        pos_token += 1
        return ids,pos_ids

class ProbeModel(nn.Module):
    def __init__(self, model, config):
        super(ProbeModel, self).__init__()
        self.model = model
        self.probe_cls_fc1 = nn.Linear(config.n_embd, config.n_embd)
        self.probe_cls_fc2 = nn.Linear(config.n_embd, config.pos_vocab_size)


    def forward(self, input_ids, position_ids=None,pos_ids = None, token_type_ids=None, labels=None, past=None, head_mask=None):
        model_outputs = self.model(input_ids, position_ids=None,pos_ids = None, token_type_ids=None, labels=None, past=None, head_mask=None)
        sem_hid_state = model_outputs[-1]
        syn_hid_states = model_outputs[-2]

        pos_logits = self.probe_cls_fc2(torch.relu(self.probe_cls_fc1(syn_hid_states)))
        #pos_logits = self.probe_cls_fc1(sem_hid_state)

        shift_pos_logits = pos_logits[..., :-1, :].contiguous()
        shift_pos_labels = pos_ids[..., 1:].contiguous()

        loss_fct = CrossEntropyLoss(ignore_index=-1)
        loss = loss_fct(shift_pos_logits.view(-1, shift_pos_logits.size(-1)),
                                shift_pos_labels.view(-1))

        return loss,shift_pos_logits

def load_tokenize_and_batchify(data_dir = '../SemSynLSTM/word_language_model/data/wikitext-2/', input_len = 128):
    """
    load dataset and return train, val, test dataset

    """
    tensor_datasets = []

    corpus = TaggedCorpus(data_dir)
    train_data = corpus.train
    val_data = corpus.valid
    test_data = corpus.test

    for dataset in [train_data, val_data, test_data]:
        ##divide data by batch, truncate to fit into batches
        n_batch = len(dataset[0]) // input_len
        input_ids = dataset[0][: n_batch * input_len].reshape(n_batch, input_len)
        pos_ids = dataset[1][: n_batch * input_len].reshape(n_batch, input_len)
        all_inputs = (input_ids, pos_ids)
        tensor_datasets.append(tuple(t for t in all_inputs))

    return tensor_datasets[0], tensor_datasets[1],tensor_datasets[2], corpus.dictionary, corpus.pos_dictionary

class WrapperLMHead(GPT2LMHeadModel):

    def __init__(self, model1, model2, config, model_option):
        super(GPT2LMHeadModel, self).__init__(config)
        self.syn_transformer = model1
        self.sem_transformer = model2
        self.model_option = model_option
        if model_option == 'gpt_2_2':
            self.lm_head = nn.Linear(2 * config.n_embd, config.vocab_size, bias=False)
        elif model_option == 'syn_only':
            self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        elif model_option == 'adverse':
            self.lm_head = nn.Linear(2 * config.n_embd, config.vocab_size, bias=False)
            self.adv_pos_head = nn.Linear(config.n_embd, config.pos_vocab_size, bias=False)
        self.pos_head = nn.Linear(config.n_embd, config.pos_vocab_size, bias=False)
        self.apply(self.init_weights)
        #self.tie_weights()

    def forward(self, input_ids,pos_ids = None, position_ids=None, token_type_ids=None, labels=None, past=None, head_mask=None):
        syn_transformer_outputs = self.syn_transformer(input_ids, position_ids=position_ids, token_type_ids=token_type_ids,
                                               past=past, head_mask=head_mask)
        syn_hidden_states = syn_transformer_outputs[0]

        #TODO set position_id to be all zero or randomly permutated? for semTransformer
        if self.model_option == 'gpt_2_2':
            sem_transformer_outputs = self.sem_transformer(input_ids, position_ids=torch.zeros_like(input_ids), token_type_ids=token_type_ids,
                                                     past=past, head_mask=head_mask)
            sem_hidden_states = sem_transformer_outputs[0]

            hidden_states = torch.cat((syn_hidden_states, sem_hidden_states), dim=-1)
        elif self.model_option == 'syn_only':
            hidden_states = syn_hidden_states
        lm_logits1 = self.lm_head(hidden_states)
        pos_logits = self.pos_head(syn_hidden_states)
        outputs = (lm_logits1,) + (hidden_states,syn_hidden_states, sem_hidden_states)
        if labels is not None and pos_ids is not None:
            # Shift so that tokens < n predict n

            shift_logits = lm_logits1[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            shift_pos_logits = pos_logits[..., :-1, :].contiguous()
            shift_pos_ids = pos_ids[..., 1:].contiguous()
            # Flatten the tokens
            #TODO why are we ignoring -1 ?
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            loss_lm = loss_fct(shift_logits.view(-1, shift_logits.size(-1)),
                            shift_labels.view(-1))
            loss_pos = loss_fct(shift_pos_logits.view(-1, shift_pos_logits.size(-1)),
                            shift_pos_ids.view(-1))
            loss = loss_lm + loss_pos
            outputs = ((loss,loss_lm, loss_pos),) + outputs

        return outputs  # (loss), lm_logits, presents, (all hidden_states), (attentions)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_name", type=str, help="pretrained_model.")
    parser.add_argument("--model_option", type=str, default='gpt-2-2', help="pretrained_model.")
    parser.add_argument("--do_train", action='store_true', help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true', help="Whether to run eval on the dev set.")
    parser.add_argument("--do_probe", action='store_true', help="Whether to run probing.")
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument('--data_dir', type=str, default='/home/xiongyi/dataxyz/repos/SemSynLSTM/word_language_model/data/wikitext-2/')
    parser.add_argument('--seed', type=int, default=12)
    parser.add_argument('--num_train_epochs', type=int, default=3)
    parser.add_argument('--train_batch_size', type=int, default=8)
    parser.add_argument('--eval_batch_size', type=int, default=16)
    parser.add_argument('--max_grad_norm', type=int, default=1)
    parser.add_argument('--learning_rate', type=float, default=6.25e-5)
    parser.add_argument('--warmup_proportion', type=float, default=0.002)
    parser.add_argument('--lr_schedule', type=str, default='warmup_linear')
    parser.add_argument('--weight_decay', type=float, default=0.01)
    parser.add_argument('--lm_coef', type=float, default=0.9)
    parser.add_argument('--n_valid', type=int, default=374)

    timenow = datetime.datetime.now().strftime("%b%d%H%M")
    model_option = 'gpt_2_2'
    outdir = model_option + timenow
    args = parser.parse_args(['--output_dir', outdir,'--do_probe','--num_train_epochs', '10', '--model_option',model_option])
    #args = parser.parse_args(['--output_dir', './tmp', '--do_eval', '--model_name', 'gpt2'])
    print(args)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    n_gpu = torch.cuda.device_count()
    logger.info("device: {}, n_gpu {}".format(device, n_gpu))


    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    # This loading functions also add new tokens and embeddings called `special tokens`
    # These new embeddings will be fine-tuned on the RocStories dataset
    #special_tokens = ['_start_', '_delimiter_']
    #special_tokens_ids = list(tokenizer.convert_tokens_to_ids(token) for token in special_tokens)

    # Compute the max input length for the Transformer
    input_length = 128
    data_dir = '../SemSynLSTM/word_language_model/data/wikitext-2/' if args.data_dir is None else args.data_dir
    train_set, val_set, test_set, dictionary, pos_dictionary = load_tokenize_and_batchify(data_dir, input_length)

    # Prepare inputs tensors and dataloaders

    train_data = TensorDataset(*train_set)
    train_sampler = RandomSampler(train_data)
    train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=32)

    eval_data = TensorDataset(*val_set)
    eval_sampler = SequentialSampler(eval_data)
    eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=32)

    config = GPT2Config(n_positions=256,n_ctx=256, n_layer=8,n_head=8, n_embd= 256)
    config.vocab_size = dictionary.__len__()
    config.pos_vocab_size = pos_dictionary.__len__()
    config.n_ctx = input_length
    config.n_positions = input_length
    model1 = GPT2Model(config=config)

    #TODO: GPTWithPOSPredicting
    model2 = GPT2Model(config=config)

    #TODO: Wrapp2Transformers together and add a LM head
    model = WrapperLMHead(model1, model2, config, args.model_option)
    model.to(device)

    # TODO: Load and encode the datasets

    logger.info("Encoding dataset...")

    # Prepare optimizer
    if args.do_train:
        param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        num_train_optimization_steps = len(train_dataloader) * args.num_train_epochs
        optimizer = AdamW(optimizer_grouped_parameters,
                          lr=args.learning_rate,
                          #max_grad_norm=args.max_grad_norm,
                          weight_decay=args.weight_decay)
                          #t_total=num_train_optimization_steps)

    if args.do_train:
        train_results = {}
        nb_tr_steps, tr_loss, exp_average_loss = 0, 0, None
        model.train()
        for epoch in trange(int(args.num_train_epochs), desc="Epoch"):
            ###eval on eval set
            model.eval()
            nb_eval_steps, nb_eval_examples = 0, 0
            log_probs_sum = 0
            perp = 0.0
            average_loss = np.array([0.0,0.0,0.0])
            for batch in tqdm(eval_dataloader, desc="Evaluating"):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_pos_ids = batch

                with torch.no_grad():
                    loss, loss_lm, loss_pos = model(input_ids, pos_ids = input_pos_ids, labels=input_ids)[0]
                    loss = loss.detach().cpu().numpy()
                    loss_lm = loss_lm.detach().cpu().numpy()
                    loss_pos = loss_pos.detach().cpu().numpy()
                    perp_batch = np.exp(loss_lm)
                    perp += perp_batch
                    average_loss += np.array([loss, loss_lm, loss_pos])
                nb_eval_steps += 1
            perp /= nb_eval_steps
            average_loss /= nb_eval_steps
            print('loss, loss_lm, loss_pos', average_loss,'perp ', perp, 'epoch ', epoch)
            train_results[epoch]= (perp, average_loss)

            model.train()

            tr_loss = 0
            nb_tr_steps = 0
            tqdm_bar = tqdm(train_dataloader, desc="Training")
            for step, batch in enumerate(tqdm_bar):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_pos_ids = batch
                loss = model(input_ids, labels=input_ids, pos_ids = input_pos_ids)[0][0]
                #breakpoint()
                #loss = args.lm_coef * losses[0] + losses[1]
                loss.backward()
                optimizer.step()
                optimizer.zero_grad()
                tr_loss += loss.item()
                exp_average_loss = loss.item() if exp_average_loss is None else 0.7 * exp_average_loss + 0.3 * loss.item()
                nb_tr_steps += 1
                tqdm_bar.desc = "Training loss: {:.2e} ".format(exp_average_loss)

    # Save a trained model
    if args.do_train:
        # Save a trained model, configuration and tokenizer
        model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self

        # If we save using the predefined names, we can load using `from_pretrained`
        output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
        output_config_file = os.path.join(args.output_dir, CONFIG_NAME)

        torch.save(model_to_save.state_dict(), output_model_file)
        model_to_save.config.to_json_file(output_config_file)
        #tokenizer.save_vocabulary(args.output_dir)

        # Load a trained model and vocabulary that you have fine-tuned
        model = GPT2LMHeadModel.from_pretrained(args.output_dir)
        #tokenizer = OpenAIGPTTokenizer.from_pretrained(args.output_dir)
        model.to(device)
        print (train_results)
    if args.do_eval:
        model.eval()
        nb_eval_steps, nb_eval_examples = 0, 0
        log_probs_sum=0
        perp = 0
        for batch in tqdm(eval_dataloader, desc="Evaluating"):
            batch = tuple(t.to(device) for t in batch)
            input_ids, input_pos_ids = batch

            with torch.no_grad():
                loss = model(input_ids, labels= input_ids, pos_ids=input_pos_ids)[0].detach().cpu().numpy()
                perp_batch = np.exp(loss)
                perp += perp_batch
            nb_eval_steps += 1

        perp /= nb_eval_steps
        # perp_word = perp / 128
        print (perp)
        result = {'eval_perp': perp}
        logger.info("***** Eval results *****")
        logger.info("'eval_perp' = %s", str(result['eval_perp']))
        # output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        # with open(output_eval_file, "w") as writer:
        #     logger.info("***** Eval results *****")
        #     for key in sorted(result.keys()):
        #         logger.info("  %s = %s", key, str(result[key]))
        #         writer.write("%s = %s\n" % (key, str(result[key])))

    if args.do_probe:

        ##load model (how???)
        model_path = '/home/xiongyi/dataxyz/repos/pytorch-pretrained-BERT/examples/gpt2_2_jul22/pytorch_model.bin_double'
        model.load_state_dict(torch.load(model_path))
        ##Add a mlp to the representation

        probe_model = ProbeModel(model, config)
        probe_model.to(device)
        ##train and eval
        all_param = list(probe_model.named_parameters())
        param_probe = [(n, p) for n, p in all_param if 'probe_cls' in n]
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_probe if not any(nd in n for nd in no_decay)],
             'weight_decay': 0.01},
            {'params': [p for n, p in param_probe if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate,
                          # max_grad_norm=args.max_grad_norm,
                          weight_decay=args.weight_decay)
        # t_total=num_train_optimization_steps)
        train_results = {}
        nb_tr_steps, tr_loss, exp_average_loss = 0, 0, None
        for epoch in trange(int(args.num_train_epochs), desc="Epoch"):
            ###eval on eval set
            probe_model.eval()
            nb_eval_steps, nb_eval_examples = 0, 0
            average_loss = 0
            average_acc = 0
            for batch in tqdm(eval_dataloader, desc="Evaluating"):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_pos_ids = batch

                with torch.no_grad():
                    #breakpoint()
                    loss = probe_model(input_ids, labels=input_ids, pos_ids=input_pos_ids)[0].detach().cpu().numpy()
                    pos_logits = probe_model(input_ids, labels=input_ids, pos_ids=input_pos_ids)[1].detach().cpu().numpy()
                    predicted_labels = np.argmax(pos_logits, -1)
                    correct_rate = np.mean(predicted_labels == input_pos_ids.detach().cpu().numpy()[:,1:])
                    average_acc += correct_rate
                    average_loss += loss
                nb_eval_steps += 1
            average_loss /= nb_eval_steps
            average_acc /= nb_eval_steps
            print('loss', average_loss,' acc_rate ', average_acc, ' epoch ', epoch)
            train_results[epoch] = (average_loss, average_acc)

            probe_model.train()

            tr_loss = 0
            nb_tr_steps = 0
            tqdm_bar = tqdm(train_dataloader, desc="Training")
            for step, batch in enumerate(tqdm_bar):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_pos_ids = batch
                loss = probe_model(input_ids, labels=input_ids, pos_ids=input_pos_ids)[0]
                # breakpoint()
                # loss = args.lm_coef * losses[0] + losses[1]
                loss.backward()
                optimizer.step()
                optimizer.zero_grad()
                tr_loss += loss.item()
                exp_average_loss = loss.item() if exp_average_loss is None else 0.7 * exp_average_loss + 0.3 * loss.item()
                nb_tr_steps += 1
                tqdm_bar.desc = "Training loss: {:.2e}".format(exp_average_loss)
if __name__ == '__main__':
    main()
