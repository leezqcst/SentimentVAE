import numpy as np
import tensorflow as tf

from config import cfg
import utils


class EncoderDecoderModel(object):

    '''The variational encoder-decoder model.'''

    def __init__(self, vocab, training, generator=False):
        self.vocab = vocab
        self.training = training
        self.global_step = tf.get_variable('global_step', shape=[],
                                           initializer=tf.zeros_initializer,
                                           trainable=False)
        self.summary_op = None
        self.summaries = []

        with tf.name_scope('input'):
            # left-aligned data:  <sos> w1 w2 ... w_T <eos> <pad...>
            self.data = tf.placeholder(tf.int32, [cfg.batch_size, None], name='data')
            # sentences with word dropout
            self.data_dropped = tf.placeholder(tf.int32, [cfg.batch_size, None],
                                               name='data_dropped')
            # sentence lengths
            self.lengths = tf.placeholder(tf.int32, [cfg.batch_size], name='lengths')
            self.labels = tf.placeholder(tf.int32, [cfg.batch_size], name='labels')

        embs = self.word_embeddings(self.data)
        embs_dropped = self.word_embeddings(self.data_dropped, reuse=True)
        embs_labels = self.label_embeddings(self.labels)

        with tf.name_scope('reverse-embeddings'):
            embs_reversed = tf.reverse_sequence(embs, self.lengths, 1)

        with tf.name_scope('expand-label-dims'):
            # Compensate for words being shifted by 1
            length = tf.shape(embs_reversed)[1]
            embs_labels = tf.expand_dims(embs_labels, 1)
            self.embs_labels = tf.tile(embs_labels, [1, length, 1])

        if generator:
            self.z = tf.placeholder(tf.float32, [cfg.batch_size, cfg.latent_size])
        else:
            with tf.name_scope('concat_words_and_labels'):
                embs_words = embs_reversed[:, 1:, :]
                embs_words_with_labels = tf.concat(2, [embs_words,
                                                       self.embs_labels[:, 1:, :]])

            self.z_mean, z_logvar = self.encoder(embs_words_with_labels)

            if cfg.variational:
                with tf.name_scope('reparameterize'):
                    eps = tf.random_normal([cfg.batch_size, cfg.latent_size])
                    self.z = self.z_mean + tf.mul(tf.sqrt(tf.exp(z_logvar)), eps)
            else:
                self.z = self.z_mean

        with tf.name_scope('transform-z'):
            z = utils.highway(self.z, layer_size=2, f=tf.nn.elu)
            self.z_transformed = utils.linear(z, cfg.latent_size, True,
                                              scope='transform_z')

        with tf.name_scope('concat_words-labels-z'):
            # Concatenate dropped word embeddings, label embeddingd and 'z'
            zt = tf.expand_dims(self.z_transformed, 1)
            zt = tf.tile(zt, [1, length, 1])
            decode_embs = tf.concat(2, [embs_dropped, self.embs_labels, zt])

        output = self.decoder(decode_embs, z)
        if cfg.mutual_info:
            pencoder_embs = tf.concat(2, [embs_dropped, self.embs_labels])
            zo_mean, zo_logvar = self.output_encoder(pencoder_embs, output)

        # shift left the input to get the targets
        with tf.name_scope('left-shift'):
            targets = tf.concat(1, [self.data[:, 1:], tf.zeros([cfg.batch_size, 1],
                                                               tf.int32)])
        with tf.name_scope('mle-cost'):
            nll_per_word = self.mle_loss(output, targets)
            avg_lengths = tf.cast(tf.reduce_mean(self.lengths), tf.float32)
            self.nll = tf.reduce_sum(nll_per_word) / cfg.batch_size
            self.perplexity = tf.exp(self.nll/avg_lengths)
            self.summaries.append(tf.scalar_summary('perplexity', self.perplexity))
            self.summaries.append(tf.scalar_summary('cost_mle', self.nll))
        with tf.name_scope('kld-cost'):
            if not cfg.variational or generator:
                self.kld = tf.zeros([])
            else:
                self.kld = tf.reduce_sum(self.kld_loss(self.z_mean, z_logvar)) / \
                           cfg.batch_size
            self.summaries.append(tf.scalar_summary('cost_kld', tf.reduce_mean(self.kld)))
            self.kld_weight = cfg.anneal_max * tf.sigmoid((12 / cfg.anneal_bias)
                                             * (self.global_step - (cfg.anneal_bias / 2)))
            self.summaries.append(tf.scalar_summary('weight_kld', self.kld_weight))
        with tf.name_scope('mutinfo-cost'):
            if not cfg.mutual_info:
                self.mutinfo = tf.zeros([])
            else:
                self.mutinfo = tf.reduce_sum(self.mutinfo_loss(self.z,
                                                               zo_mean, zo_logvar)) / \
                               cfg.batch_size
            self.summaries.append(tf.scalar_summary('cost_mutinfo',
                                                    tf.reduce_mean(self.mutinfo)))

        with tf.name_scope('cost'):
            self.cost = self.nll + (self.kld_weight * self.kld) + \
                        (cfg.mutinfo_weight * self.mutinfo)

        if training and not generator:
            self.train_op = self.train(self.cost)
        else:
            self.train_op = tf.no_op()

    def rnn_cell(self, num_layers):
        '''Return a multi-layer RNN cell.'''
        return tf.nn.rnn_cell.MultiRNNCell([tf.nn.rnn_cell.GRUCell(cfg.hidden_size)
                                            for _ in range(num_layers)])

    def label_embeddings(self, labels):
        '''Lookup embeddings for labels'''
        with tf.device('/cpu:0'), tf.variable_scope('Label-Embeddings'):
            init = tf.random_uniform_initializer(-1.0, 1.0)
            self.label_embedding = tf.get_variable('label_embedding',
                                                   [len(self.vocab.labels),
                                                    cfg.label_emb_size],
                                                   initializer=init)
            embeds = tf.nn.embedding_lookup(self.label_embedding,
                                            labels - min(self.vocab.labels),
                                            name='label_embedding_lookup')
        return embeds

    def word_embeddings(self, inputs, reuse=None):
        '''Look up word embeddings for the input indices.'''
        with tf.device('/cpu:0'), tf.variable_scope("Embeddings", reuse=reuse):
            init = tf.random_uniform_initializer(-1.0, 1.0)
            self.embedding = tf.get_variable('word_embedding', [len(self.vocab.vocab),
                                                                cfg.word_emb_size],
                                             initializer=init)
            embeds = tf.nn.embedding_lookup(self.embedding, inputs,
                                            name='word_embedding_lookup')
        return embeds

    def encoder(self, inputs, scope=None):
        '''Encode sentence and return a latent representation.'''
        with tf.variable_scope(scope or "Encoder"):
            outputs, _ = tf.nn.dynamic_rnn(self.rnn_cell(cfg.num_layers), inputs,
                                           sequence_length=self.lengths-1,
                                           swap_memory=True, dtype=tf.float32)
            outputs = tf.reshape(outputs, [-1, cfg.hidden_size])
            outputs = utils.highway(outputs, f=tf.nn.elu, scope='encoder_output_highway')
            outputs = utils.linear(outputs, cfg.latent_size, True,
                                   scope='outputs_transform')
            flat_input = tf.reshape(inputs, [-1, inputs.get_shape()[2].value])
            weights = utils.linear(tf.concat(1, [flat_input, outputs]), cfg.latent_size,
                                   True, scope='outputs_attention')
            outputs = tf.reshape(outputs, [cfg.batch_size, -1, cfg.latent_size])
            weights = tf.reshape(weights, [cfg.batch_size, -1, cfg.latent_size])
            weights = tf.nn.softmax(weights, 1)
            z = tf.nn.elu(tf.reduce_sum(outputs * weights, [1]))
            z_mean = utils.linear(z, cfg.latent_size, True, scope='encoder_z_mean')
            z_logvar = utils.linear(z, cfg.latent_size, True, scope='encoder_z_logvar')
        return z_mean, z_logvar

    def decoder(self, inputs, z):
        '''Use the latent representation and word inputs to predict next words.'''
        with tf.variable_scope("Decoder"):
            initial = []
            for i in range(cfg.num_layers):
                initial.append(tf.nn.tanh(utils.linear(z, cfg.hidden_size, True, 0.0,
                                                       scope='decoder_initial%d' % i)))
            self.decode_initial = tuple(initial)
            self.decode_cell = self.rnn_cell(cfg.num_layers)
            output, _ = tf.nn.dynamic_rnn(self.decode_cell, inputs,
                                          initial_state=self.decode_initial,
                                          sequence_length=self.lengths-1,
                                          swap_memory=True, dtype=tf.float32)
        return output

    def output_encoder(self, inputs, output):
        '''Encode decoder outputs and return a proposal posterior.'''
        return self.encoder(tf.concat(2, [inputs, output]), scope="PosteriorProposal")

    def mle_loss(self, outputs, targets):
        '''Maximum likelihood estimation loss.'''
        mask = tf.cast(tf.greater(targets, 0, name='targets_mask'), tf.float32)
        output = tf.reshape(tf.concat(1, outputs), [-1, cfg.hidden_size])
        with tf.variable_scope("MLE_Softmax"):
            xinit = tf.contrib.layers.xavier_initializer()
            self.softmax_w = tf.get_variable("W", [len(self.vocab.vocab),
                                                   cfg.hidden_size], initializer=xinit)
            self.softmax_b = tf.get_variable("b", [len(self.vocab.vocab)],
                                             initializer=tf.zeros_initializer)
        if self.training and cfg.softmax_samples < len(self.vocab.vocab):
            targets = tf.reshape(targets, [-1, 1])
            mask = tf.reshape(mask, [-1])
            loss = tf.nn.sampled_softmax_loss(self.softmax_w, self.softmax_b, output,
                                              targets, cfg.softmax_samples,
                                              len(self.vocab.vocab))
            loss *= mask
        else:
            logits = tf.nn.bias_add(tf.matmul(output, tf.transpose(self.softmax_w),
                                              name='softmax_transform_mle'),
                                    self.softmax_b)
            loss = tf.nn.seq2seq.sequence_loss_by_example([logits],
                                                          [tf.reshape(targets, [-1])],
                                                          [tf.reshape(mask, [-1])])
        loss = tf.reshape(loss, [cfg.batch_size, -1])
        return loss

    def kld_loss(self, z_mean, z_logvar):
        '''KL divergence loss.'''
        z_var = tf.exp(z_logvar)
        z_mean_sq = tf.square(z_mean)
        kld_loss = 0.5 * tf.reduce_sum(z_var + z_mean_sq - 1 - z_logvar, 1)
        return kld_loss

    def mutinfo_loss(self, z, z_mean, z_logvar):
        '''Mutual information loss. We want to maximize the likelihood of z in the
           Gaussian represented by z_mean, z_logvar.'''
        z_var = tf.exp(z_logvar)
        z_epsilon = tf.square(z - z_mean)
        return 0.5 * tf.reduce_sum(tf.log(2 * np.pi) + z_logvar + z_epsilon / z_var, 1)

    def train(self, cost):
        '''Generic training helper'''
        self.lr = tf.get_variable("lr", shape=[], initializer=tf.zeros_initializer,
                                  trainable=False)
        optimizer = utils.get_optimizer(self.lr, cfg.optimizer)
        tvars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)
        grads = tf.gradients(cost, tvars)
        #tmp = []
        #for g, v in zip(grads, tvars):
        #    tmp.append(tf.Print(tf.reduce_sum(g), [tf.sqrt(tf.reduce_sum(tf.square(g)))],
        #                        v.op.name, summarize=10))
        #a = tf.reduce_mean(tf.pack(tmp)) * 0.0
        if cfg.max_grad_norm > 0:
        #    grads[0] += a
            grads, _ = tf.clip_by_global_norm(grads, cfg.max_grad_norm)
        return optimizer.apply_gradients(list(zip(grads, tvars)),
                                         global_step=self.global_step)

    def assign_lr(self, session, lr):
        '''Update the learning rate.'''
        session.run(tf.assign(self.lr, lr))

    def summary(self):
        if self.summary_op is None:
            self.summary_op = tf.merge_summary(self.summaries)
        return self.summary_op
