import sys
import os
import time

import numpy as np
import tensorflow as tf

#from src.controller import Controller
from src.utils import get_train_ops
from src.common_ops import stack_lstm


class GeneralController(object):
  def __init__(self,
               num_layers=4,
               out_filters=[3,2,3,3],
               search_count=False,
               lstm_size=32,
               lstm_num_layers=2,
               lstm_keep_prob=1.0,
               tanh_constant=None,
               temperature=None,
               lr_init=1e-3,
               lr_dec_start=0,
               lr_dec_every=100,
               lr_dec_rate=0.9,
               l2_reg=0,
               entropy_weight=None,
               clip_mode=None,
               grad_bound=None,
               use_critic=False,
               bl_dec=0.999,
               optim_algo="adam",
               sync_replicas=False,
               num_aggregate=None,
               num_replicas=None,
               skip_target=0.8,
               skip_weight=0.5,
               name="controller",
               *args,
               **kwargs):

    print("-" * 80)
    print("Building ConvController")

    #self.search_for = search_for
    #self.search_whole_channels = search_whole_channels
    self.num_layers = num_layers
    #self.num_branches = num_branches
    self.out_filters = out_filters
    self.search_count = search_count

    self.lstm_size = lstm_size
    self.lstm_num_layers = lstm_num_layers 
    self.lstm_keep_prob = lstm_keep_prob
    self.tanh_constant = tanh_constant
    self.temperature = temperature
    self.lr_init = lr_init
    self.lr_dec_start = lr_dec_start
    self.lr_dec_every = lr_dec_every
    self.lr_dec_rate = lr_dec_rate
    self.l2_reg = l2_reg
    self.entropy_weight = entropy_weight
    self.clip_mode = clip_mode
    self.grad_bound = grad_bound
    self.use_critic = use_critic
    self.bl_dec = bl_dec

    self.skip_target = skip_target
    self.skip_weight = skip_weight

    self.optim_algo = optim_algo
    self.sync_replicas = sync_replicas
    self.num_aggregate = num_aggregate
    self.num_replicas = num_replicas
    self.name = name

    self._create_params()
    self._build_sampler()
    self._build_trainer()
    self._build_train_op()

  def _create_params(self):
    initializer = tf.random_uniform_initializer(minval=-0.1, maxval=0.1)
    with tf.variable_scope(self.name, initializer=initializer):
      with tf.variable_scope("lstm"):
        self.w_lstm = []
        for layer_id in range(self.lstm_num_layers):
          with tf.variable_scope("lstm_layer_{}".format(layer_id)):
            w = tf.get_variable(
              "w", [2 * self.lstm_size, 4 * self.lstm_size])
            self.w_lstm.append(w)

      # g_emb: input
      self.g_emb = tf.get_variable("g_emb", [1, self.lstm_size])
      self.w_emb = {"start": [], "count": []}
      with tf.variable_scope("emb"):
         for layer_ in range(self.num_layers):
          with tf.variable_scope("layer_{}".format(layer_)):
            self.w_emb["start"].append(tf.get_variable(
              "w_start", [self.out_filters[layer_], self.lstm_size]));
            self.w_emb["count"].append(tf.get_variable(
              "w_count", [self.out_filters[layer_] - 1, self.lstm_size]));

      # count: how many output_channels to take
      self.w_soft = {"start": [], "count": []}
      with tf.variable_scope("softmax"):
        #for branch_id in xrange(self.num_branches):
        for layer_ in range(self.num_layers):
          #with tf.variable_scope("branch_{}".format(branch_id)):
          with tf.variable_scope("layer_{}".format(layer_)):
            self.w_soft["start"].append(tf.get_variable(
              "w_start", [self.lstm_size, self.out_filters[layer_]]));
            if self.search_count:
              self.w_soft["count"].append(tf.get_variable(
                "w_count", [self.lstm_size, self.out_filters[layer_] - 1]));

      with tf.variable_scope("attention"):
        self.w_attn_1 = tf.get_variable("w_1", [self.lstm_size, self.lstm_size])
        self.w_attn_2 = tf.get_variable("w_2", [self.lstm_size, self.lstm_size])
        self.v_attn = tf.get_variable("v", [self.lstm_size, 1])

  def _build_sampler(self):
    """Build the sampler ops and the log_prob ops."""

    print("-" * 80)
    print("Build controller sampler")
    anchors = []
    anchors_w_1 = []

    arc_seq = []
    entropys = []
    log_probs = []
    skip_count = []
    skip_penaltys = []
    masks = []

    prev_c = [tf.zeros([1, self.lstm_size], tf.float32) for _ in
              range(self.lstm_num_layers)]
    prev_h = [tf.zeros([1, self.lstm_size], tf.float32) for _ in
              range(self.lstm_num_layers)]
    inputs = self.g_emb
    skip_targets = tf.constant([1.0 - self.skip_target, self.skip_target],
                               dtype=tf.float32)
    for layer_id in range(self.num_layers):
      ###
      ### for each layer, sample num_branches operations
      ###
      #for branch_id in range(self.num_branches):
      next_c, next_h = stack_lstm(inputs, prev_c, prev_h, self.w_lstm)
      prev_c, prev_h = next_c, next_h
      #logit = tf.matmul(next_h[-1], self.w_soft["start"][branch_id]) # out_filter x 1
      logit = tf.matmul(next_h[-1], self.w_soft["start"][layer_id]) # out_filter x 1
      if self.temperature is not None:
        logit /= self.temperature
      if self.tanh_constant is not None:
        logit = self.tanh_constant * tf.tanh(logit)
      # start: a random number from 0 to out_filters[i]
      start = tf.multinomial(logit, 1)
      start = tf.to_int32(start)
      start = tf.reshape(start, [1])
      arc_seq.append(start)
      log_prob = tf.nn.sparse_softmax_cross_entropy_with_logits(
        logits=logit, labels=start)
      log_probs.append(log_prob)
      entropy = tf.stop_gradient(log_prob * tf.exp(-log_prob))
      entropys.append(entropy)
      # inputs: get a row slice of [out_filter[i], lstm_size]
      #inputs = tf.nn.embedding_lookup(self.w_emb["start"][branch_id], start) 
      inputs = tf.nn.embedding_lookup(self.w_emb["start"][layer_id], start) 

      next_c, next_h = stack_lstm(inputs, prev_c, prev_h, self.w_lstm)
      prev_c, prev_h = next_c, next_h

      if self.search_count:
        #logit = tf.matmul(next_h[-1], self.w_soft["count"][branch_id])
        logit = tf.matmul(next_h[-1], self.w_soft["count"][layer_id])
        if self.temperature is not None:
          logit /= self.temperature
        if self.tanh_constant is not None:
          logit = self.tanh_constant * tf.tanh(logit)
        # mask: a boolean list of length out_filter[i]-1 
        # that is true for all <=out_filter[i]-start elements
        mask = tf.range(0, limit=self.out_filters[layer_id]-1, delta=1, dtype=tf.int32)
        mask = tf.reshape(mask, [1, self.out_filters[layer_id] - 1])
        mask = tf.less_equal(mask, self.out_filters[layer_id]-1 - start)
        masks.append([mask, start])
        # tf.where: for index of false in mask, x will be replaced with y
        logit = tf.where(mask, x=logit, y=tf.fill(tf.shape(logit), -np.inf))
        # logit: >out_filter[i]-start will be masked to 0
        # e.g.: if start is 3 and out_filter[i] is 10, then 8,9 will be masked to 0
        count = tf.multinomial(logit, 1)
        count = tf.to_int32(count)
        count = tf.reshape(count, [1])
        arc_seq.append(count + 1)
        log_prob = tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=logit, labels=count)
        log_probs.append(log_prob)
        entropy = tf.stop_gradient(log_prob * tf.exp(-log_prob))
        entropys.append(entropy)
        # inputs: get a row slice of [out_filter[i]-1, lstm_size]
        #inputs = tf.nn.embedding_lookup(self.w_emb["count"][branch_id], count)
        inputs = tf.nn.embedding_lookup(self.w_emb["count"][layer_id], count)

        next_c, next_h = stack_lstm(inputs, prev_c, prev_h, self.w_lstm)
        prev_c, prev_h = next_c, next_h

      ###
      ### sample the connections, unless the first layer
      ### the number `skip` of each layer grows as layer_id grows
      ###
      if layer_id > 0:
        query = tf.concat(anchors_w_1, axis=0)  # layer_id x lstm_size
        # w_attn_2: lstm_size x lstm_size
        query = tf.tanh(query + tf.matmul(next_h[-1], self.w_attn_2)) # query: layer_id x lstm_size
        ## P(Layer j is an input to layer i) = sigmoid(v^T %*% tanh(W_prev ∗ h_j + W_curr ∗ h_i))
        query = tf.matmul(query, self.v_attn) # query: layer_id x 1
        logit = tf.concat([-query, query], axis=1) # logit: layer_id x 2
        if self.temperature is not None:
          logit /= self.temperature
        if self.tanh_constant is not None:
          logit = self.tanh_constant * tf.tanh(logit)

        skip = tf.multinomial(logit, 1)  # layer_id x 1 of booleans
        skip = tf.to_int32(skip)
        skip = tf.reshape(skip, [layer_id])
        arc_seq.append(skip)

        skip_prob = tf.sigmoid(logit)
        kl = skip_prob * tf.log(skip_prob / skip_targets)
        kl = tf.reduce_sum(kl)
        skip_penaltys.append(kl)

        log_prob = tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=logit, labels=skip)
        #log_probs.append(tf.reduce_sum(log_prob, keep_dims=True))
        log_probs.append(tf.reshape(tf.reduce_sum(log_prob),[-1]))

        entropy = tf.stop_gradient(
          #tf.reduce_sum(log_prob * tf.exp(-log_prob), keep_dims=True))
          tf.reshape(tf.reduce_sum(log_prob * tf.exp(-log_prob)), [-1]) )
        entropys.append(entropy)

        skip = tf.to_float(skip)
        skip = tf.reshape(skip, [1, layer_id])
        skip_count.append(tf.reduce_sum(skip))
        inputs = tf.matmul(skip, tf.concat(anchors, axis=0))
        inputs /= (1.0 + tf.reduce_sum(skip))
      else:
        inputs = self.g_emb

      anchors.append(next_h[-1])
      # next_h: 1 x lstm_size
      # anchors_w_1: 1 x lstm_size
      anchors_w_1.append(tf.matmul(next_h[-1], self.w_attn_1))  

    arc_seq = tf.concat(arc_seq, axis=0)
    self.sample_arc = tf.reshape(arc_seq, [-1])

    entropys = tf.stack(entropys)
    self.sample_entropy = tf.reduce_sum(entropys)

    log_probs = tf.stack(log_probs)
    self.sample_log_prob = tf.reduce_sum(log_probs)

    skip_count = tf.stack(skip_count)
    self.skip_count = tf.reduce_sum(skip_count)

    skip_penaltys = tf.stack(skip_penaltys)
    self.skip_penaltys = tf.reduce_mean(skip_penaltys)

  
  def _build_trainer(self):
    print("-" * 80)
    print("Build controller trainer")
    anchors = []
    anchors_w_1 = []

    ops_each_layer = 2 if self.search_count else 1
    total_arc_len = sum([ops_each_layer] + [ ops_each_layer+i for i in range(1, self.num_layers) ])
    self.total_arc_len = total_arc_len
    self.input_arc = [tf.placeholder(shape=(), dtype=tf.int32, name='arc_{}'.format(i))
      for i in range(total_arc_len)]
    entropys = []
    log_probs = []
    skip_count = []
    skip_penaltys = []
    masks = []

    prev_c = [tf.zeros([1, self.lstm_size], tf.float32) for _ in
              range(self.lstm_num_layers)]
    prev_h = [tf.zeros([1, self.lstm_size], tf.float32) for _ in
              range(self.lstm_num_layers)]
    inputs = self.g_emb
    skip_targets = tf.constant([1.0 - self.skip_target, self.skip_target],
                               dtype=tf.float32)
    
    arc_pointer = 0
    for layer_id in range(self.num_layers):
      ###
      ### for each layer, sample num_branches operations
      ###
      #for branch_id in range(self.num_branches):
      next_c, next_h = stack_lstm(inputs, prev_c, prev_h, self.w_lstm)
      prev_c, prev_h = next_c, next_h
      logit = tf.matmul(next_h[-1], self.w_soft["start"][layer_id]) # out_filter x 1
      if self.temperature is not None:
        logit /= self.temperature
      if self.tanh_constant is not None:
        logit = self.tanh_constant * tf.tanh(logit)
      # start: a random number from 0 to out_filters[i]
      start = self.input_arc[arc_pointer]
      start = tf.reshape(start, [1])

      log_prob = tf.nn.sparse_softmax_cross_entropy_with_logits(
        logits=logit, labels=start)
      log_probs.append(log_prob)
      entropy = tf.stop_gradient(log_prob * tf.exp(-log_prob))
      entropys.append(entropy)
      # inputs: get a row slice of [out_filter[i], lstm_size]
      #inputs = tf.nn.embedding_lookup(self.w_emb["start"][branch_id], start) 
      inputs = tf.nn.embedding_lookup(self.w_emb["start"][layer_id], start) 

      next_c, next_h = stack_lstm(inputs, prev_c, prev_h, self.w_lstm)
      prev_c, prev_h = next_c, next_h

      if self.search_count:
        #logit = tf.matmul(next_h[-1], self.w_soft["count"][branch_id])
        logit = tf.matmul(next_h[-1], self.w_soft["count"][layer_id])
        if self.temperature is not None:
          logit /= self.temperature
        if self.tanh_constant is not None:
          logit = self.tanh_constant * tf.tanh(logit)
        # mask: a boolean list of length out_filter[i]-1 
        # that is true for all <=out_filter[i]-start elements
        mask = tf.range(0, limit=self.out_filters[layer_id]-1, delta=1, dtype=tf.int32)
        mask = tf.reshape(mask, [1, self.out_filters[layer_id] - 1])
        mask = tf.less_equal(mask, self.out_filters[layer_id]-1 - start)
        masks.append([mask, start])
        # tf.where: for index of false in mask, x will be replaced with y
        logit = tf.where(mask, x=logit, y=tf.fill(tf.shape(logit), -np.inf))
        # logit: >out_filter[i]-start will be masked to 0
        # e.g.: if start is 3 and out_filter[i] is 10, then 8,9 will be masked to 0
        count = self.input_arc[arc_pointer+1]
        count = tf.reshape(count, [1])
        count = count - 1
        #arc_seq.append(count + 1)
        log_prob = tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=logit, labels=count)
        log_probs.append(log_prob)
        entropy = tf.stop_gradient(log_prob * tf.exp(-log_prob))
        entropys.append(entropy)
        # inputs: get a row slice of [out_filter[i]-1, lstm_size]
        #inputs = tf.nn.embedding_lookup(self.w_emb["count"][branch_id], count)
        inputs = tf.nn.embedding_lookup(self.w_emb["count"][layer_id], count)
        next_c, next_h = stack_lstm(inputs, prev_c, prev_h, self.w_lstm)
        prev_c, prev_h = next_c, next_h

      ###
      ### sample the connections, unless the first layer
      ### the number `skip` of each layer grows as layer_id grows
      ###
      if layer_id > 0:
        query = tf.concat(anchors_w_1, axis=0)  # layer_id x lstm_size
        # w_attn_2: lstm_size x lstm_size
        query = tf.tanh(query + tf.matmul(next_h[-1], self.w_attn_2)) # query: layer_id x lstm_size
        ## P(Layer j is an input to layer i) = sigmoid(v^T %*% tanh(W_prev ∗ h_j + W_curr ∗ h_i))
        query = tf.matmul(query, self.v_attn) # query: layer_id x 1
        logit = tf.concat([-query, query], axis=1) # logit: layer_id x 2
        if self.temperature is not None:
          logit /= self.temperature
        if self.tanh_constant is not None:
          logit = self.tanh_constant * tf.tanh(logit)

        skip = self.input_arc[(arc_pointer+ops_each_layer) : (arc_pointer+ops_each_layer + layer_id)]
        #print(layer_id, (arc_pointer+2), (arc_pointer+2 + layer_id), skip)
        skip = tf.reshape(skip, [layer_id])
        
        skip_prob = tf.sigmoid(logit)
        kl = skip_prob * tf.log(skip_prob / skip_targets)
        kl = tf.reduce_sum(kl)
        skip_penaltys.append(kl)

        log_prob = tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=logit, labels=skip)
        log_probs.append(tf.reshape(tf.reduce_sum(log_prob),[-1]))

        entropy = tf.stop_gradient(
          tf.reshape(tf.reduce_sum(log_prob * tf.exp(-log_prob)), [-1]) )
        entropys.append(entropy)

        skip = tf.to_float(skip)
        skip = tf.reshape(skip, [1, layer_id])
        skip_count.append(tf.reduce_sum(skip))
        inputs = tf.matmul(skip, tf.concat(anchors, axis=0))
        inputs /= (1.0 + tf.reduce_sum(skip))
        
      else:
        inputs = self.g_emb

      anchors.append(next_h[-1])
      # next_h: 1 x lstm_size
      # anchors_w_1: 1 x lstm_size
      anchors_w_1.append(tf.matmul(next_h[-1], self.w_attn_1))
      arc_pointer += ops_each_layer + layer_id

    entropys = tf.stack(entropys)
    self.onehot_entropy = tf.reduce_sum(entropys)

    log_probs = tf.stack(log_probs)
    self.onehot_log_prob = tf.reduce_sum(log_probs)

    skip_count = tf.stack(skip_count)
    self.onehot_skip_count = tf.reduce_sum(skip_count)

    skip_penaltys = tf.stack(skip_penaltys)
    self.onehot_skip_penaltys = tf.reduce_mean(skip_penaltys)


  def _build_train_op(self):
    self.reward = tf.Variable(0.0, dtype=tf.float32, trainable=False)

    normalize = tf.to_float(self.num_layers * (self.num_layers - 1) / 2)
    self.skip_rate = tf.to_float(self.skip_count) / normalize

    if self.entropy_weight is not None:
      self.reward += self.entropy_weight * self.onehot_entropy

    self.onehot_log_prob = tf.reduce_sum(self.onehot_log_prob)
    self.baseline = tf.Variable(0.0, dtype=tf.float32, trainable=False)
    baseline_update = tf.assign_sub(
      self.baseline, (1 - self.bl_dec) * (self.baseline - self.reward))

    with tf.control_dependencies([baseline_update]):
      self.reward = tf.identity(self.reward)

    self.loss = self.onehot_log_prob * (self.reward - self.baseline)
    if self.skip_weight is not None:
      self.loss += self.skip_weight * self.skip_penaltys

    self.train_step = tf.Variable(
        0, dtype=tf.int32, trainable=False, name="train_step")
    tf_variables = [var
        for var in tf.trainable_variables() if var.name.startswith(self.name)]
    print("-" * 80)
    for var in tf_variables:
      print(var)

    self.train_op, self.lr, self.grad_norm, self.optimizer = get_train_ops(
      self.loss,
      tf_variables,
      self.train_step,
      clip_mode=self.clip_mode,
      grad_bound=self.grad_bound,
      l2_reg=self.l2_reg,
      lr_init=self.lr_init,
      lr_dec_start=self.lr_dec_start,
      lr_dec_every=self.lr_dec_every,
      lr_dec_rate=self.lr_dec_rate,
      optim_algo=self.optim_algo,
      sync_replicas=self.sync_replicas,
      num_aggregate=self.num_aggregate,
      num_replicas=self.num_replicas)
