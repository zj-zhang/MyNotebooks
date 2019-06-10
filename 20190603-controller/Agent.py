""" an agent reacts to a policy actions and give feedback
"""
from BioNAS.Controller.state_space import StateSpace


def build_dag():
	ss = StateSpace()
	ss.add_layer(0, ['How', 'What', 'Why'])
	ss.add_layer(1, ['are', 'is'])
	ss.add_layer(2, ['I', 'you', 'it'])
	ss.add_layer(3, ['doing', 'going', 'coming'])
	return ss

class Agent(object):
	def __init__(self, dag, default_score=-1., ops_each_layer=1):
		self.dag = dag
		self.dag_score = {}
		self.ops_each_layer = ops_each_layer
		dag_path = ['']
		for layer in dag:
			tmp = []
			for candidate in layer:
				for p in dag_path:
					tmp.append(' '.join([p, candidate]).strip())
			dag_path = tmp
		for x in dag_path:
			self.dag_score[x] = default_score 

	def alter_path_score(self, dag_path, score):
		self.dag_score[dag_path] = score


	def decode_arc_seq(self, arc_seq, skip_concat=lambda x,y: x+y):
		num_layers = len(self.dag)
		arc_pointer = 0
		dag_path = ''
		for layer_id in range(num_layers):
			if layer_id == 0:
				dag_path = ' '.join([dag_path, self.dag[layer_id][arc_seq[arc_pointer]] ]).strip()
			else:
				dag_path = ' '.join([dag_path, self.dag[layer_id][arc_seq[arc_pointer]] ]).strip()
			arc_pointer += self.ops_each_layer + layer_id
		return dag_path

	def get_reward(self, dag_path):
		try:
			return self.dag_score[dag_path]
		except KeyError:
			return -2.