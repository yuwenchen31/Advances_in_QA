"""
This module implements the Paragraph Selector from the paper, Section 3.1
"""

import pandas as pd
import torch
from transformers import BertTokenizer, BertModel
from sklearn.utils import shuffle
from sklearn.model_selection import train_test_split
import os,sys,inspect
from tqdm import tqdm
from sklearn.metrics import recall_score, precision_score, f1_score
current_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir) 
from utils import HotPotDataHandler


def make_training_data(data,
                       tokenizer=BertTokenizer.from_pretrained('bert-base-uncased')):
    """
    #TODO docstring
    Make a dataframe with training data for selecting relevant paragraphs
    Each entry in the dataframe has three columns:
    1. id -- ID of the question
    2. label -- 0 (unrelated) or 1 (related)
    3. tokens -- token IDs of query and paragraph
    :param data: question ID, supporting facts, question, and paragraphs
    :type data: list(tuple(str, list(str), str, list(list(str, list(str)))))
    :param tokenizer: default: BertTokenizer(bert-base-uncased)
    :return: DataFame
    """

    labels = []
    datapoints = []
    for point in tqdm(data):
        for para in point[3]:
            # Label is 1: if paragraph title is in supporting facts, otherwise 0
            labels.append([int(para[0] in point[1])])
            # automatically prefixes [CLS] and appends [SEP]
            token_ids = tokenizer.encode( point[2] + " [SEP] " + ("").join(para[1]) ) #TODO limit to 512 tokens
            datapoints.append(token_ids)

    result = torch.Tensor(list(zip(datapoints, labels))) #TODO make this work (are tensors really the way to go?)
    result = result.T
    print(result.shape)
    print(type(result))
    input("shape and type of the data") #CLEANUP
    print(result)
    return result

class ParagraphSelector():
    """
    TODO: write docstring
    """
    
    def __init__(self,
                 model_path=None,
                 tokenizer=None,
                 encoder_model=None):
        """
        TODO: write docstring
        """
        self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased') if not tokenizer else tokenizer

        class ParagraphSelectorNet(torch.nn.Module):
            """
            TODO: write docstring
            """
            def __init__(self, input_size=768, output_size=1):
                super(ParagraphSelectorNet, self).__init__()
                self.encoder_model = BertModel.from_pretrained('bert-base-uncased',
                                                               output_hidden_states=True,
                                                               output_attentions=True) if not encoder_model else encoder_model
                self.linear  = torch.nn.Linear(input_size, output_size)

            def forward(self, token_ids):

                # [-2] is all_hidden_states
                # [-1] is the last hidden state (list of sentences)
                # first [0] - first (and only) sentence
                # second [0] - first ([CLS]) token of the sentence
                from pprint import pprint
                pprint(token_ids)
                print(type(token_ids))
                input("type of token_ids")
                token_ids = torch.tensor(list(token_ids))
                print(type(token_ids[0]))
                input("token_ids[0]")
                print(type(self.encoder_model(token_ids)))
                input("type of the encoder_model output")
                print(self.encoder_model(token_ids).shape) #CLEANUP
                input("shape of the encoder_model output")
                print(self.encoder_model(token_ids)[-2][-1][0][0])
                input("encoder_model CLS token?")
                embedding = self.encoder_model(token_ids)[-2][-1][0][0]
                output = self.linear(embedding)
                output = torch.sigmoid(output)
                return output 
            
        self.net = ParagraphSelectorNet()
        if model_path:
            try:
                self.net.load_state_dict(torch.load(model_path))
            except FileNotFoundError as e:
                print(e, model_path)

    def train(self, train_data, labels, epochs=10, learning_rate=0.0001):
        """
        TODO: write docstring
        """
        # Use Binary Cross Entropy as a loss function instead of MSE
        # There are papers on why MSE is bad for classification
        criterion = torch.nn.BCELoss()
        optimizer = torch.optim.Adam(self.net.parameters(), lr=learning_rate)

        losses = []
        inputs = train_data[1] # ["tokens"]
        labels = train_data[2] # ["label"]


        # Set the network into train mode
        self.net.train()

        # put the net on the GPU if possible
        if torch.cuda.is_available():
            self.net = self.net.to(torch.device('cuda'))

        print("Training...")

        # Iterate over the epochs
        N = len(train_data)
        for epoch in range(epochs):
            print('Epoch %d/%d' % (epoch + 1, epochs))
            for i in range(0, N, batch_size):
                inputs = train_data[i:i+batch_size] # select one batch of data
                labels = labels[i:i+batch_size] # and the corresponding labels

                optimizer.zero_grad()
                outputs = self.net(inputs) # encode and apply linear layer
                loss = criterion(outputs, labels)
                loss.backward(retain_graph=True)
                losses.append(loss.item())
                optimizer.step()

        return losses
    
    def evaluate(self, data, threshold=0.1):
        """
        TODO: write docstring
        """
        all_true = []
        all_pred = []
        ids = []
        
        for point in data:
            context = self.make_context(point, threshold) #point[2] are the paragraphs, point[1] is the query
            para_true = []
            para_pred = []
            for para in point[3]:
                para_true.append(para[0] in point[1])
                para_pred.append(para in context)
            all_true.append(para_true)
            all_pred.append(para_pred)
            ids.append(point[0])
        
        # Flatten the lists so they can be passed to the precision and recall funtions
        all_true_flattened = [point for para in all_true for point in para]
        all_pred_flattened = [point for para in all_pred for point in para]
        
        precision = precision_score(all_true_flattened, all_pred_flattened)
        recall = recall_score(all_true_flattened, all_pred_flattened)
        f1 = f1_score(all_true_flattened, all_pred_flattened)
        
        return precision, recall, f1, ids, all_true, all_pred
    
    def predict(self, p):
        """
        Given the encoding of a paragraph (query + paragraph),
        return the score that the model predicts for that paragraph
        :param p: encoding of the paragraph (along with the query), as described in the paper
        :return: score between 0 and 1 for that paragraph
        """
        self.net.eval()
        score = self.net(p)
        return score
    
    def make_context(self, datapoint, threshold=0.1):
        """
        TODO: write docstring
        
        Parameters: paragraphs - [[p1_title, [p1_s1, p1_s2 ...]],
                                  [p2_title, [p2_s1, p2_s2, ...]],
                                   ...]
                    query - the query as a string
                    threshold - a float between zero and one;
                                paragraphs that get a score above the
                                threshold, become part of the context
        Output: context: [[p1_title, [p1_s1, p1_s2 ...]],
                          [p2_title, [p2_s1, p2_s2, ...]],
                           ...]
        """
        context = []

        for p in datapoint[3]:
            # p[0] is the paragraph title, p[1] is the list of sentences in the paragraph
            encoded_p = torch.tensor([self.tokenizer.encode(
                                      datapoint[2] + " [SEP] " + ("").join(p[1]) ) #TODO limit to 512 tokens
                                     ])
            score = self.predict(encoded_p)
            if score > threshold:
                context.append(p)
        return context
    
    def save(self, savepath):
        '''
        TODO: write docstring
        '''
        directory_name = "/".join(savepath.split('/')[:-1])
        print("Save to:", directory_name)
        if not os.path.exists(directory_name):
            os.makedirs(directory_name)
        torch.save(self.net.state_dict(), savepath)

if __name__ == "__main__":
    print("Reading data...")
    dh = HotPotDataHandler(parent_dir + "/data/hotpot_train_v1.1.json")
    data = dh.data_for_paragraph_selector()

    print("Splitting data...")
    training_data_raw, test_data_raw = train_test_split(data[:4], test_size=0.25, random_state=42, shuffle=True)
    training_data = make_training_data(training_data_raw)
    training_data = shuffle(training_data, random_state=42)

    # TODO we use DataLoader to handle batching; is this the right way?
    train_data = torch.utils.data.DataLoader(training_data, batch_size=1)
    
    print("Initilising ParagraphSelector...")
    ps = ParagraphSelector()
    losses = ps.train(train_data, epochs=1)
    
    print("Saving model...")
    ps.save(parent_dir + '/models/paragraphSelector_all.pt')
    
    print("Evaluating...")
    precision, recall, f1, ids, y_true, y_pred = ps.evaluate(test_data_raw)
    print('----------------------')
    print("Precision:", precision)
    print("Recall:", recall)
    print("F score:", f1)
    
    if not os.path.exists(parent_dir + "/models/performance/"):
        os.makedirs(parent_dir + "/models/performance/")
    
    with open(parent_dir + '/models/performance/outputs.txt', 'w', encoding='utf-8') as f:
        for i in range(len(ids)):
            f.write(ids[i] + "\t" + \
                    ','.join([str(int(j)) for j in y_true[i]]) + "\t" + \
                    ','.join([str(int(j)) for j in y_pred[i]]) + "\n")
    
    with open(parent_dir + '/models/performance/results.txt', 'w', encoding='utf-8') as f:
        f.write("Outputs in: " + parent_dir + '/models/performance/outputs.txt'+ \
               "\nPrecision: " + str(precision) + \
               "\nRecall: " + str(recall) + \
               "\nF score: " + str(f1))
        
        
