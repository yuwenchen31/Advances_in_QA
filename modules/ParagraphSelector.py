"""
This module implements the Paragraph Selector from the paper, Section 3.1
"""

import pandas as pd
import torch
from transformers import BertTokenizer, BertModel, BertPreTrainedModel, BertConfig
from sklearn.utils import shuffle
import os,sys,inspect
import math
from tqdm import tqdm
import argparse

from pprint import pprint

from sklearn.model_selection import train_test_split
from sklearn.metrics import recall_score, precision_score, f1_score, accuracy_score
current_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

from utils import HotPotDataHandler
from utils import ConfigReader
from utils import Timer

# weights for training, because we have imbalanced data:
# 80% of paragraphs are not important (= class 0) and 20% are important (class 1)
#WEIGHTS = [0.2, 0.8] #CLEANUP? We implemented downscaling instead of this loss weighting


def make_training_data(data,
                       text_length=512,
                       tokenizer=BertTokenizer.from_pretrained('bert-base-uncased')):
    """
    Make a train tensor for each datapoint.

    Note on downsampling: HotPotQA's distractor dev set has 2 relevant
    and 8 irrelevant paragraphs for each question. In order to avoid
    having unbalanced data, we take the only the first 2 irrelevant paragraphs.

    :param data: question ID, supporting facts, question, and paragraphs, 
                 as returned by HotPotDataHandler
    :type data: list(tuple(str, list(str), str, list(list(str, list(str)))))
    :param text_length: id of the pad token used to pad when
                        the paragraph is shorter then text_length
                        default is 0
    :param tokenizer: default: BertTokenizer(bert-base-uncased)

    :return: a train tensor with two columns:
                1. token_ids as returned by the tokenizer for
                   [CLS] + query + [SEP] + paragraph + [SEP]
                   (10 entries per datapoint, one of each paragraph)
                2. labels for the points - 0 if the paragraphs is
                   no relevant to the query, and 1 otherwise
    """

    neg_max = 2 # maximum number of useless paragraphs to be used per question
    labels = []
    datapoints = []
    for point in tqdm(data):
        neg_counter = 0
        for para in point[3]:
            is_useful_para = para[0] in point[1] # Label is 1: if paragraph title is in supporting facts, otherwise 0
            if not is_useful_para and neg_counter == neg_max: # enough negative examples
                continue
            else: # useful paragraph or neg_max not yet reached
                if not is_useful_para:
                    neg_counter += 1

                labels.append(float(is_useful_para))
                point_string = point[2] + " [SEP] " + ("").join(para[1])

                # automatically prefixes [CLS] and appends [SEP]
                token_ids = tokenizer.encode(point_string, max_length=512)

                # Add padding if there are fewer than text_length tokens,
                # else trim to text_length
                if len(token_ids) < text_length:
                    token_ids += [tokenizer.pad_token_id for _ in range(text_length - len(token_ids))]
                else:
                    token_ids = token_ids[:text_length]
                datapoints.append(token_ids)
        #print(sum(labels[-4:])==2) #CLEANUP

    # Turn labels and datapoints into tensors and put them together        
    label_tensor = torch.tensor(labels)
    train = torch.tensor(datapoints)
    train_tensor = torch.utils.data.TensorDataset(train, label_tensor)

    return train_tensor

class ParagraphSelector():
    """
    This class implements all that is necessary for training
    a paragraph selector model (as per the requirements in the 
    paper), predicting relevance scores for paragraph-query pairs
    and building the context for HotPotQA datapoint. Additionally,
    it also allows for saving a trained model, loading a trained 
    model from a file, and evaluating a model.
    """
    
    def __init__(self,
                 model_path,
                 tokenizer=None,
                 encoder_model=None):
        """
        #TODO update the docstring
        Initialization function for the ParagraphSelector class

        :param model_path: path to an already trained model (only
                           necessary if we want to load a pretrained
                           model)
        :param tokenizer: a tokenizer, default is BertTokenizer.from_pretrained('bert-base-uncased')
        :param encoder_model: an encoder model, default is BertModel.from_pretrained('bert-base-uncased')
        """
        self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased') if not tokenizer else tokenizer

        class ParagraphSelectorNet(BertPreTrainedModel):
            """
            A neural network for the paragraph selector.
            """
            def __init__(self, config):#, input_size=768, output_size=1):
                """
                Initialization of the encoder model and a linear layer

                :param config: config as required by BertPreTrainedModel
                """
                super(ParagraphSelectorNet, self).__init__(config)
                self.bert = BertModel(config)#('bert-base-uncased',
                                                               #output_hidden_states=True,
                                                               #output_attentions=True) if not encoder_model else encoder_model
                self.linear = torch.nn.Linear(config.hidden_size, 1)
                self.init_weights()

            def forward(self, token_ids):
                """
                Forward function of the ParagraphSelectorNet.
                Takes in token_ids corresponding to a query+paragraph
                and returns a relevance score (between 0 and 1) for
                the query and paragraph.

                :param token_ids: token_ids as returned by the tokenizer;
                                  the text that is passed to the tokenizer
                                  is constructed by [CLS] + query + [SEP] + paragraph + [SEP]
                """

                # [-2] is all_hidden_states
                # [-1] is the last hidden state (list of sentences)
                # [:,0,:] - we want for all the sentence (:),
                # only the first token (0) (this is the [CLS token]), 
                # all its dimensions (:) (768 with bert-base-uncased)

                #with torch.no_grad(): #TODO de-activate this?
                #embedding = self.bert(token_ids)[-2][-1][:, 0, :] #TODO maybe, this throws errors. in this case, look at Stalin's version below

                outputs = self.bert(token_ids)
                embedding = outputs[0][:, 0, :]

                output = self.linear(embedding)
                output = torch.sigmoid(output)
                return output
        
        # initialise a paragraph selector net and try to load
        self.config = BertConfig.from_pretrained(model_path)  # , cache_dir=args.cache_dir if args.cache_dir else None,)
        self.net = ParagraphSelectorNet.from_pretrained(model_path,
                                                        from_tf=bool(".ckpt" in model_path),
                                                        config=self.config)  # , cache_dir=args.cache_dir if args.cache_dir else None,)


    def train(self, train_data, dev_data, model_save_path,
              epochs=10, batch_size=1, learning_rate=0.0001, eval_interval=None, try_gpu=True):
        """
        Train a ParagraphSelectorNet on a training dataset.
        Binary Cross Entopy is used as the loss function.
        Adam is used as the optimizer.

        :param train_data: a tensor as returned by the make_training_data() function;
                           it has two columns:
                        a train tensor with two columns:
                            1. token_ids as returned by the tokenizer for
                               [CLS] + query + [SEP] + paragraph + [SEP]
                               (10 entries per datapoint, one of each paragraph)
                            2. labels for the points - 0 if the paragraphs is
                               no relevant to the query, and 1 otherwise
        :param epochs: number of training epochs, default is 10
        :param batch_size: batch size for the training, default is 1
        :param learning_rate: learning rate for the optimizer,
                              default is 0.0001

        :return losses: a list of losses
        :return dev_scores: a list of tuples (evaluation step, p, r, f1, acc.)
        """
        # Use Binary Cross Entropy as a loss function instead of MSE
        # There are papers on why MSE is bad for classification
        criterion = torch.nn.BCELoss()
        optimizer = torch.optim.Adam(self.net.parameters(), lr=learning_rate)

        losses = []
        dev_scores = []

        # Set the network into train mode
        self.net.train()
        
        
        cuda_is_available = torch.cuda.is_available() if try_gpu else False
        device = torch.device('cuda') if cuda_is_available else torch.device('cpu')

        # put the net on the GPU if possible
        if cuda_is_available:
            self.net = self.net.to(device)

        print("Training...")

        # (120, 250) --> batching --> (30, 4, 250)
        train_data = torch.utils.data.DataLoader(dataset = train_data, batch_size = batch_size, shuffle=True)

        c = 0  # counter over taining examples
        high_score = 0
        eval_interval = eval_interval if eval_interval else float('inf')
        batched_interval = round(eval_interval/batch_size) # number of batches needed to reach eval_interval
        a_model_was_saved_at_some_point = False

        for epoch in range(epochs):
            print('Epoch %d/%d' % (epoch + 1, epochs))

            for step, batch in enumerate(tqdm(train_data, desc="Iteration")):
                batch = [t.to(device) if t is not None else None for t in batch]
                inputs, labels = batch
                #weight_tensor = torch.Tensor([WEIGHTS[int(label)] for label in labels]).to(device) #CLEANUP?
                #criterion.weight = weight_tensor #CLEANUP?
                #print(inputs.shape) #CLEANUP

                optimizer.zero_grad()

                outputs = self.net(inputs).squeeze(1) #TODO why squeeze(1)?
                loss = criterion(outputs, labels)
                loss.backward(retain_graph=True)
                losses.append(loss.item())

                c +=1
                # Evaluate on validation set after some iterations
                if c % batched_interval == 0:
                    p, r, f1, accuracy, _, _, _ = self.evaluate(dev_data, try_gpu=try_gpu)
                    dev_scores.append((c/batched_interval, p, r, f1, accuracy))

                    measure = f1
                    if measure > high_score:
                        print(f"Better eval found with score {round(measure ,3)} (+{round(measure-high_score, 3)})")
                        high_score = measure
                        self.net.save_pretrained(model_save_path)
                        a_model_was_saved_at_some_point = True
                    else:
                        print(f"No improvement yet...")



                optimizer.step()

        if not a_model_was_saved_at_some_point: # make sure that there is a model file
            self.net.save_pretrained(model_save_path)

        return losses, dev_scores
    
    def evaluate(self, data, threshold=0.1, text_length=512, try_gpu=True):
        """
        Evaluate a trained model on a dataset.
        True labels on the evaluation datapoints are made in this function as well.

        :param data: a list of datapoints where each point has the
                     following structure:
                        (question_id, supporting_facts, query, paragraphs),
                        where question_id is a string corresponding
                              to the datapoint id in HotPotQA
                        supporting_facts is a list of strings,
                        query is a string,
                        paragraphs is a 10-element list where
                            the first element is a string
                            the second element is a list of sentences (i.e., a list of strings)

        :param threshold: a float between zero and one;
                          paragraphs that get a score above the
                          threshold, become part of the context,
                          default is 0.1
        :param text_length: text_length of the paragraph - paragraph will
                            be padded if its length is less than this value
                            and trimmed if it is more, default is 512
        :param try_gpu: boolean specifying whether to use GPU for
                        computation if GPU is available; default is True

        :return precision: precision for the model
        :return recall: recall for the model
        :return f1: f1 score for the model
        :return acc: accuracy for the model
        :return ids: list of ids of all the evaluated points
        :return all_true: true labels for the datapoints
                          list(list(boolean)), a list of datapoints
                          where each datapoint is a list of 
                          booleans; each boolean corresponds to whether
                          the corresponding paragraph is relevant to
                          the query or not
        :return all_pred: predicted labels for the datapoints
                          list(list(boolean)), a list of datapoints
                          where each datapoint is a list of 
                          booleans; each boolean corresponds to whether
                          the corresponding paragraph is relevant to
                          the query or not
        """
        all_true = []
        all_pred = []
        ids = []

        self.net.eval()
        device = torch.device('cuda') if try_gpu and torch.cuda.is_available() \
            else torch.device('cpu')
        self.net = self.net.to(device)

        for point in tqdm(data, desc="eval points"):
            context, c_indices = self.make_context(point, #point[2] are the paragraphs, point[1] is the query
                                                   threshold=threshold,
                                                   text_length=text_length,
                                                   device=device,
                                                   numerated=True) # returns original paragraph numbers
            para_true = []
            para_pred = []
            for i, para in enumerate(point[3]): # iterate over all 10 paragraphs
                para_true.append(para[0] in point[1]) # True if paragraph's title is in the supporting facts
                para_pred.append(i in c_indices) # compare indices instead of strings to avoid mismatches due to trimming
            all_true.append(para_true)
            all_pred.append(para_pred)
            ids.append(point[0])

        # Flatten the lists so they can be passed to the precision and recall funtions
        all_true_flattened = [point for para in all_true for point in para]
        all_pred_flattened = [point for para in all_pred for point in para]
        
        precision = precision_score(all_true_flattened, all_pred_flattened)
        recall = recall_score(all_true_flattened, all_pred_flattened)
        f1 = f1_score(all_true_flattened, all_pred_flattened)
        acc = accuracy_score(all_true_flattened, all_pred_flattened)
        
        return precision, recall, f1, acc, ids, all_true, all_pred
    
    def predict(self, p, device=torch.device('cpu')):
        """
        Given the token_ids of a query+paragraph for a specific paragraph,
        return the relevance score that the model predicts between the query
        and the paragraph

        :param p: token_ids as returned by the tokenizer;
                  the text that is passed to the tokenizer
                  is constructed by [CLS] + query + [SEP] + paragraph + [SEP]
        :return: score between 0 and 1 for that paragraph
        """

        # put the net and the paragraph onto the GPU if possible
        #cuda_is_available = torch.cuda.is_available()
        #device = torch.device('cuda') if cuda_is_available else torch.device('cpu')
        #if cuda_is_available:
        #    self.net = self.net.to(device)
        #    p = p.to(device) #CLEANUP?
        #self.net.eval() #CLEANUP?

        p = p.to(device)
        score = self.net(p)
        return score
    
    def make_context(self, datapoint, threshold=0.1,
                     context_length=512, text_length=512,
                     device=torch.device('cpu'),
                     numerated=False):
        """
        Given a datapoint from HotPotQA, build the context for it.
        The context consists of all paragraphs included in that
        datapoint which have a relevance score higher than a 
        specific value (threshold) to the query of that datapoint.
         
        :param datapoint: datapoint for which to make context
                          shape: (question_id, supporting_facts, query, paragraphs, answer),
                                where question_id is a string corresponding
                                      to the datapoint id in HotPotQA
                                supporting_facts is a list of strings,
                                query is a string,
                                paragraphs is a 10-element list of lists where
                                    the first element is a string
                                    the second element is a list of sentences (i.e., a list of strings)
        :param threshold: a float between zero and one;
                          paragraphs that get a score above the
                          threshold, become part of the context,
                          default is 0.1
        :param text_length: text_length of the paragraph - paragraph will
                            be padded if its length is less than this value
                            and trimmed if it is more, default is 512.
                            The trimming happens by paragraph, so that all
                            paragraphs in the context are of equal length (text_length / num_paragraphs)
        :param device: device for processing; default is 'cpu'

        :return context: the context for the datapoint
                shape: [ [[p1_title], [p1_s1, p1_s2, ...]],
                         [[p2_title], [p2_s1, p2_s2, ...]],
                        ...]
                        The p*_title and p*_s* are strings.
        """

        # for the case that a user picks a limit greater than BERT's max length
        if text_length > 512:
            print("Maximum input length for Paragraph Selector exceeded; continuing with 512.")
            text_length = 512
        if context_length > 512:
            print("Maximum context length exceeded; continuing with 512.")
            context_length = 512


        context = []
        para_indices = []

        # encode header and paragraph individually to be able to join just paragraphs
        # automatically prefixes [CLS] and appends [SEP]
        query_token_ids = self.tokenizer.encode(datapoint[2],
                                                 max_length=512) # to avoid warnings
        """ SELECT PARAGRAPHS """

        #print(f"\nin ParagraphSelector.make_context():") #CLEANUP
        #print(f"id: {datapoint[0]}") #CLEANUP

        for i, p in enumerate(datapoint[3]):
            header_token_ids = self.tokenizer.encode(p[0],
                                                   max_length=512, # to avoid warnings
                                                   add_special_tokens=False)
            # encode sentences individually
            sentence_token_ids = [self.tokenizer.encode(sentence,
                                                   max_length=512, # to avoid warnings
                                                   add_special_tokens=False)
                              for sentence in p[1]]

            token_ids = query_token_ids \
                      + header_token_ids \
                      + [token for sent in sentence_token_ids for token in sent]
            token_ids[-1] = self.tokenizer.sep_token_id  # make sure that it ends with a SEP

            # Add padding if there are fewer than text_length tokens,
            if len(token_ids) < text_length:
                token_ids += [self.tokenizer.pad_token_id for _ in range(text_length - len(token_ids))]
            else: # else trim to text_length
                token_ids = token_ids[:text_length]
                token_ids[-1] = self.tokenizer.sep_token_id  # make sure that it still ends with a SEP

            # do the actual prediction & decision
            encoded_p = torch.tensor([token_ids])
            #print(f"in ParagraphSelector.make_context: shape of encoded_p: {encoded_p.shape}") #CLEANUP
            score = self.predict(encoded_p, device=device)
            if score > threshold:
                # list[list[int], list[list[int]]]
                # no [CLS] or [SEP] here
                # WATCH OUT! this doesn't necessarily have text_length! (context will be padded in the Encoder)
                context.append([header_token_ids, sentence_token_ids])
                para_indices.append(i)


        """ TRIM EACH PARAGRAPH OF THE CONTEXT """
        # shorten each paragraph so that the combined length is not too big
        # and decode so that strings are returned
        #TODO maybe extract this to a function
        trimmed_context = [] # new data structure because we prioritise computing time over memory usage
        cut_off_point = 0 if not context else math.floor(context_length/len(context)) # roughly cut to an even length

        for i, (header, para) in enumerate(context):
            if len(header) >= cut_off_point:
                trimmed_context.append([self.tokenizer.decode(header[:cut_off_point]), []])
                continue # don't even look at the paragraph
            else:
                pos = len(header) # the header counts towards the paragraph!
                trimmed_context.append([self.tokenizer.decode(header), []])

            for sentence in para:
                if pos + len(sentence) > cut_off_point: # we need to trim
                    s = sentence[:cut_off_point - pos]
                    s = self.tokenizer.decode(s) # re-convert token IDs to strings
                    if len(s) != 0: # to soothe the Flair tagger's "ACHTUNG" statement
                        trimmed_context[i][1].append(s)
                    break # don't continue to loop over further sentences of this paragraph
                else:
                    s = self.tokenizer.decode(sentence)
                    trimmed_context[i][1].append(s) # append non-trimmed sentence to the context
                    pos += len(sentence) # go to the next sentence

        return (trimmed_context, para_indices) if numerated else trimmed_context

    def save(self, savepath):
        '''
        Save the trained model to a file.

        :param savepath: relative path to where the model
                         should be saved, including filename(?)
        '''
        directory_name = "/".join(savepath.split('/')[:-1])
        print("Save to:", directory_name)
        if not os.path.exists(directory_name):
            os.makedirs(directory_name)
        torch.save(self.net.state_dict(), savepath)
