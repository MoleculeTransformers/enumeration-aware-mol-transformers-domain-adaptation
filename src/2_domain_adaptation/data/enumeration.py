"""
Script that performs Enumeration based augmentation for chemical SMILES
Implementation borrow from: https://github.com/EBjerrum/SMILES-enumeration
"""
from __future__ import division, print_function, unicode_literals

import threading

import numpy as np
import pandas as pd
from rdkit import Chem
from tqdm import tqdm

np.random.seed(123)


class Iterator(object):
    """Abstract base class for data iterators.
    # Arguments
        n: Integer, total number of samples in the dataset to loop over.
        batch_size: Integer, size of a batch.
        shuffle: Boolean, whether to shuffle the data between epochs.
        seed: Random seeding for data shuffling.
    """

    def __init__(self, n, batch_size, shuffle, seed):
        self.n = n
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.batch_index = 0
        self.total_batches_seen = 0
        self.lock = threading.Lock()
        self.index_generator = self._flow_index(n, batch_size, shuffle, seed)
        if n < batch_size:
            raise ValueError("Input data length is shorter than batch_size\nAdjust batch_size")

    def reset(self):
        self.batch_index = 0

    def _flow_index(self, n, batch_size=32, shuffle=False, seed=None):
        # Ensure self.batch_index is 0.
        self.reset()
        while 1:
            if seed is not None:
                np.random.seed(seed + self.total_batches_seen)
            if self.batch_index == 0:
                index_array = np.arange(n)
                if shuffle:
                    index_array = np.random.permutation(n)

            current_index = (self.batch_index * batch_size) % n
            if n > current_index + batch_size:
                current_batch_size = batch_size
                self.batch_index += 1
            else:
                current_batch_size = n - current_index
                self.batch_index = 0
            self.total_batches_seen += 1
            yield (
                index_array[current_index : current_index + current_batch_size],  # noqa
                current_index,
                current_batch_size,
            )

    def __iter__(self):
        # Needed if we want to do something like:
        # for x, y in data_gen.flow(...):
        return self

    def __next__(self, *args, **kwargs):
        return self.next(*args, **kwargs)


class SmilesIterator(Iterator):
    """Iterator yielding data from a SMILES array.
    # Arguments
        x: Numpy array of SMILES input data.
        y: Numpy array of targets data.
        smiles_data_generator: Instance of `SmilesEnumerator`
            to use for random SMILES generation.
        batch_size: Integer, size of a batch.
        shuffle: Boolean, whether to shuffle the data between epochs.
        seed: Random seed for data shuffling.
        dtype: dtype to use for returned batch. Set to keras.backend.floatx if using Keras
    """

    def __init__(
        self,
        x,
        y,
        smiles_data_generator,
        batch_size=32,
        shuffle=False,
        seed=None,
        dtype=np.float32,
    ):
        if y is not None and len(x) != len(y):
            raise ValueError(
                "X (images tensor) and y (labels) should have the same length.                    Found: X.shape ="
                f" {np.asarray(x).shape}, y.shape = {np.asarray(y).shape}"
            )

        self.x = np.asarray(x)

        self.y = np.asarray(y) if y is not None else None
        self.smiles_data_generator = smiles_data_generator
        self.dtype = dtype
        super(SmilesIterator, self).__init__(x.shape[0], batch_size, shuffle, seed)

    def next(self):
        """For python 2.x.
        # Returns
            The next batch.
        """
        # Keeps under lock only the mechanism which advances
        # the indexing of each batch.
        with self.lock:
            index_array, current_index, current_batch_size = next(self.index_generator)
        # The transformation of images is not under thread lock
        # so it can be done in parallel
        batch_x = np.zeros(
            tuple([current_batch_size] + [self.smiles_data_generator.pad, self.smiles_data_generator._charlen]),
            dtype=self.dtype,
        )
        for i, j in enumerate(index_array):
            smiles = self.x[j : j + 1]  # noqa
            x = self.smiles_data_generator.transform(smiles)
            batch_x[i] = x

        return batch_x if self.y is None else (batch_x, self.y[index_array])


class SmilesEnumerator(object):
    """SMILES Enumerator, vectorizer and devectorizer

    #Arguments
        charset: string containing the characters for the vectorization
        can also be generated via the .fit() method
        pad: Length of the vectorization
        leftpad: Add spaces to the left of the SMILES
        isomericSmiles: Generate SMILES containing information about stereogenic centers
        enum: Enumerate the SMILES during transform
        canonical: use canonical SMILES during transform (overrides enum)
    """

    def __init__(
        self,
        charset="@C)(=cOn1S2/H[N]\\",
        pad=120,
        leftpad=True,
        isomericSmiles=True,
        enum=True,
        canonical=False,
    ):
        self._charset = None
        self.charset = charset
        self.pad = pad
        self.leftpad = leftpad
        self.isomericSmiles = isomericSmiles
        self.enumerate = enum
        self.canonical = canonical

    @property
    def charset(self):
        return self._charset

    @charset.setter
    def charset(self, charset):
        self._charset = charset
        self._charlen = len(charset)
        self._char_to_int = {c: i for i, c in enumerate(charset)}
        self._int_to_char = dict(enumerate(charset))

    def fit(self, smiles, extra_chars=None, extra_pad=5):
        """Performs extraction of the charset and length of a SMILES datasets and sets self.pad and self.charset

        #Arguments
            smiles: Numpy array or Pandas series containing smiles as strings
            extra_chars: List of extra chars to add to the charset (e.g. "\\\\" when "/" is present)
            extra_pad: Extra padding to add before or after the SMILES vectorization
        """
        if extra_chars is None:
            extra_chars = []
        charset = set("".join(list(smiles)))
        self.charset = "".join(charset.union(set(extra_chars)))
        self.pad = max(len(smile) for smile in smiles) + extra_pad

    def randomize_smiles(self, smiles):
        """Perform a randomization of a SMILES string
        must be RDKit sanitizable"""
        m = Chem.MolFromSmiles(smiles)
        ans = list(range(m.GetNumAtoms()))
        np.random.shuffle(ans)
        nm = Chem.RenumberAtoms(m, ans)
        return Chem.MolToSmiles(nm, canonical=self.canonical, isomericSmiles=self.isomericSmiles)

    def transform(self, smiles):
        """Perform an enumeration (randomization) and vectorization of a Numpy array of smiles strings
        #Arguments
            smiles: Numpy array or Pandas series containing smiles as strings
        """
        one_hot = np.zeros((smiles.shape[0], self.pad, self._charlen), dtype=np.int8)
        errors = 0
        if self.leftpad:
            for i, ss in enumerate(tqdm(smiles)):
                if self.enumerate:
                    ss = self.randomize_smiles(ss)
                l = len(ss)  # noqa
                diff = self.pad - l
                for j, c in enumerate(ss):
                    try:
                        one_hot[i, j + diff, self._char_to_int[c]] = 1
                    except Exception:
                        errors += 1
                        break
        else:
            for i, ss in enumerate(tqdm(smiles)):
                if self.enumerate:
                    ss = self.randomize_smiles(ss)
                for j, c in enumerate(ss):
                    try:
                        one_hot[i, j, self._char_to_int[c]] = 1
                    except Exception:
                        errors += 1
                        break

        print(f"errors: {errors}")
        return one_hot

    def reverse_transform(self, vect):
        """Performs a conversion of a vectorized SMILES to a smiles strings
        charset must be the same as used for vectorization.
        #Arguments
            vect: Numpy array of vectorized SMILES.
        """
        smiles = []
        for v in vect:
            # mask v
            v = v[v.sum(axis=1) == 1]
            # Find one hot encoded index with argmax, translate to char and join to string
            smile = "".join(self._int_to_char[i] for i in v.argmax(axis=1))
            smiles.append(smile)
        return np.array(smiles)

    def enumerate_smiles(self, data_reader, smiles_col, replication_count=2, random_pairs=False, rand_proba=0.0):
        """
        Performs enumeration augmentation on the canonical molecular SMILES

        Args:
            dataset (_type_): dataframe containing molecular SMILSS
            smiles_col (_type_): column corresponding to molecular SMILES
            replication_count (int, optional): Number of enumerations for each CHEMICAL SMILE. Defaults to 2.
        """
        smiles = np.repeat(getattr(data_reader.dataset, smiles_col), replication_count)
        self.fit(smiles, extra_chars=["\\"])
        v = self.transform(smiles)
        transformed = self.reverse_transform(v)

        # print(len(v), len(original_smiles), len(transformed))
        is_enumerated = [1] * len(smiles)
        if random_pairs:
            assert len(smiles) == len(transformed), "The length of augmented SMILES must equal original SMILES"
            for idx, _ in enumerate(tqdm(smiles)):
                if round(np.random.uniform(), 1) > rand_proba:
                    continue
                transformed[idx] = np.random.choice(smiles)
                is_enumerated[idx] = 0
        return transformed, list(smiles), is_enumerated

    def enumerate_smiles_hard_neg(
        self, data_reader, smiles_col, replication_count=2, random_pairs=False, rand_proba=0.0
    ):
        """
        Performs enumeration augmentation on the canonical molecular SMILES

        Args:
            dataset (_type_): dataframe containing molecular SMILSS
            smiles_col (_type_): column corresponding to molecular SMILES
            replication_count (int, optional): Number of enumerations for each CHEMICAL SMILE. Defaults to 2.
        """
        smiles = np.repeat(getattr(data_reader.dataset, smiles_col), replication_count)
        self.fit(smiles, extra_chars=["\\"])
        v = self.transform(smiles)
        transformed = self.reverse_transform(v)
        hard_negatives = [-1] * len(smiles)

        if random_pairs:
            assert len(smiles) == len(transformed), "The length of augmented SMILES must equal original SMILES"
            for idx, smile in enumerate(tqdm(smiles)):
                hard_neg = np.random.choice(smiles)
                while hard_neg == smile:
                    hard_neg = np.random.choice(smiles)
                hard_negatives[idx] = hard_neg

        return pd.DataFrame(
            {
                "sent1": smiles,
                "sent0": transformed,
                "hard_neg": hard_negatives,
            }
        )
