import json
from tqdm import tqdm
import collections
import tokenizations.offical_tokenization as tokenization
import os
import copy

SPIECE_UNDERLINE = '▁'


def moving_span_for_ans(start_position, end_position, context, ans_text, mov_limit=5):
    # 前后mov_limit个char搜索最优answer_span
    count_i = 0
    start_position_moved = copy.deepcopy(start_position)
    while context[start_position_moved:end_position + 1] != ans_text \
            and count_i < mov_limit \
            and start_position_moved - 1 >= 0:
        start_position_moved -= 1
        count_i += 1
    end_position_moved = copy.deepcopy(end_position)

    if context[start_position_moved:end_position + 1] == ans_text:
        return start_position_moved, end_position

    while context[start_position:end_position_moved + 1] != ans_text \
            and count_i < mov_limit and end_position_moved + 1 < len(context):
        end_position_moved += 1
        count_i += 1

    if context[start_position:end_position_moved + 1] == ans_text:
        return start_position, end_position_moved

    return start_position, end_position


def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer,
                         orig_answer_text):
    """Returns tokenized answer spans that better match the annotated answer."""

    # The SQuAD annotations are character based. We first project them to
    # whitespace-tokenized words. But then after WordPiece tokenization, we can
    # often find a "better match". For example:
    #
    #   Question: What year was John Smith born?
    #   Context: The leader was John Smith (1895-1943).
    #   Answer: 1895
    #
    # The original whitespace-tokenized answer will be "(1895-1943).". However
    # after tokenization, our tokens will be "( 1895 - 1943 ) .". So we can match
    # the exact answer, 1895.
    #
    # However, this is not always possible. Consider the following:
    #
    #   Question: What country is the top exporter of electornics?
    #   Context: The Japanese electronics industry is the lagest in the world.
    #   Answer: Japan
    #
    # In this case, the annotator chose "Japan" as a character sub-span of
    # the word "Japanese". Since our WordPiece tokenizer does not split
    # "Japanese", we just use "Japanese" as the annotation. This is fairly rare
    # in SQuAD, but does happen.
    tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

    for new_start in range(input_start, input_end + 1):
        for new_end in range(input_end, new_start - 1, -1):
            text_span = " ".join(doc_tokens[new_start:(new_end + 1)])
            if text_span == tok_answer_text:
                return (new_start, new_end)

    return (input_start, input_end)


def _check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""

    # Because of the sliding window approach taken to scoring documents, a single
    # token can appear in multiple documents. E.g.
    #  Doc: the man went to the store and bought a gallon of milk
    #  Span A: the man went to the
    #  Span B: to the store and bought
    #  Span C: and bought a gallon of
    #  ...
    #
    # Now the word 'bought' will have two scores from spans B and C. We only
    # want to consider the score with "maximum context", which we define as
    # the *minimum* of its left and right context (the *sum* of left and
    # right context will always be the same, of course).
    #
    # In the example the maximum context for 'bought' would be span C since
    # it has 1 left context and 3 right context, while span B has 4 left context
    # and 0 right context.
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span.start + doc_span.length - 1
        if position < doc_span.start:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span.start
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span.length
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


def json2features(input_file, output_files, tokenizer, is_training=False, max_query_length=64,
                  max_seq_length=512, doc_stride=128, max_ans_length=256):
    unans = 0
    yes_no_ans = 0
    with open(input_file, 'r') as f:
        train_data = json.load(f)
        train_data = train_data['data']

    def _is_chinese_char(cp):
        if ((cp >= 0x4E00 and cp <= 0x9FFF) or  #
                (cp >= 0x3400 and cp <= 0x4DBF) or  #
                (cp >= 0x20000 and cp <= 0x2A6DF) or  #
                (cp >= 0x2A700 and cp <= 0x2B73F) or  #
                (cp >= 0x2B740 and cp <= 0x2B81F) or  #
                (cp >= 0x2B820 and cp <= 0x2CEAF) or
                (cp >= 0xF900 and cp <= 0xFAFF) or  #
                (cp >= 0x2F800 and cp <= 0x2FA1F)):  #
            return True

        return False

    def is_fuhao(c):
        if c == '。' or c == '，' or c == '！' or c == '？' or c == '；' or c == '、' or c == '：' or c == '（' or c == '）' \
                or c == '－' or c == '~' or c == '「' or c == '《' or c == '》' or c == ',' or c == '」' or c == '"' or c == '“' or c == '”' \
                or c == '$' or c == '『' or c == '』' or c == '—' or c == ';' or c == '。' or c == '(' or c == ')' or c == '-' or c == '～' or c == '。' \
                or c == '‘' or c == '’' or c == ':' or c == '=' or c == '￥':
            return True
        return False

    def _tokenize_chinese_chars(text):
        """Adds whitespace around any CJK character."""
        output = []
        for char in text:
            cp = ord(char)
            if _is_chinese_char(cp) or is_fuhao(char):
                if len(output) > 0 and output[-1] != SPIECE_UNDERLINE:
                    output.append(SPIECE_UNDERLINE)
                output.append(char)
                output.append(SPIECE_UNDERLINE)
            else:
                output.append(char)
        return "".join(output)

    def is_whitespace(c):
        if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F or c == SPIECE_UNDERLINE:
            return True
        return False

    # to examples
    examples = []
    mis_match = 0
    for article in tqdm(train_data):
        for para in article['paragraphs']:
            context = para['context']
            context_chs = _tokenize_chinese_chars(context)
            for qas in para['qas']:
                qid = qas['id']
                ques_text = qas['question']
                doc_tokens = []
                char_to_word_offset = []
                prev_is_whitespace = True

                for c in context_chs:
                    if is_whitespace(c):
                        prev_is_whitespace = True
                    else:
                        if prev_is_whitespace:
                            doc_tokens.append(c)
                        else:
                            doc_tokens[-1] += c
                        prev_is_whitespace = False
                    if c != SPIECE_UNDERLINE:
                        char_to_word_offset.append(len(doc_tokens) - 1)

                start_position_final = None
                end_position_final = None
                ans_text = None
                if is_training:
                    if len(qas["answers"]) > 1:
                        raise ValueError("For training, each question should have exactly 0 or 1 answer.")
                    if 'is_impossible' in qas and (qas['is_impossible'] == 'true'):  # in CJRC it is 'true'
                        unans += 1
                        start_position_final = -1
                        end_position_final = -1
                        ans_text = ""
                    elif qas['answers'][0]['answer_start'] == -1:  # YES,NO
                        yes_no_ans += 1
                        assert qas['answers'][0]['text'] in {'YES', 'NO'}
                        if qas['answers'][0]['text'] == 'YES':
                            start_position_final = -2
                            end_position_final = -2
                            ans_text = "[YES]"
                        elif qas['answers'][0]['text'] == 'NO':
                            start_position_final = -3
                            end_position_final = -3
                            ans_text = "[NO]"
                    else:
                        ans_text = qas['answers'][0]['text']
                        if len(ans_text) > max_ans_length:
                            continue
                        start_position = qas['answers'][0]['answer_start']
                        end_position = start_position + len(ans_text) - 1

                        # if context[start_position:end_position + 1] != ans_text:
                        #     start_position, end_position = moving_span_for_ans(start_position, end_position, context,
                        #                                                        ans_text, mov_limit=5)

                        while context[start_position] == " " or context[start_position] == "\t" or \
                                context[start_position] == "\r" or context[start_position] == "\n":
                            start_position += 1

                        start_position_final = char_to_word_offset[start_position]
                        end_position_final = char_to_word_offset[end_position]

                        if doc_tokens[start_position_final] in {"。", "，", "：", ":", ".", ","}:
                            start_position_final += 1

                        actual_text = "".join(doc_tokens[start_position_final:(end_position_final + 1)])
                        cleaned_answer_text = "".join(tokenization.whitespace_tokenize(ans_text))

                        if actual_text != cleaned_answer_text:
                            print(actual_text, 'V.S', cleaned_answer_text)
                            mis_match += 1

                examples.append({'doc_tokens': doc_tokens,
                                 'orig_answer_text': context,
                                 'qid': qid,
                                 'question': ques_text,
                                 'answer': ans_text,
                                 'start_position': start_position_final,
                                 'end_position': end_position_final})

    print('examples num:', len(examples))
    print('mis_match:', mis_match)
    print('no answer:', unans)
    print('yes no answer:', yes_no_ans)
    os.makedirs('/'.join(output_files[0].split('/')[0:-1]), exist_ok=True)
    json.dump(examples, open(output_files[0], 'w'))

    # to features
    features = []
    unique_id = 1000000000
    for (example_index, example) in enumerate(tqdm(examples)):
        query_tokens = tokenizer.tokenize(example['question'])
        if len(query_tokens) > max_query_length:
            query_tokens = query_tokens[0:max_query_length]

        tok_to_orig_index = []
        orig_to_tok_index = []
        all_doc_tokens = []
        for (i, token) in enumerate(example['doc_tokens']):
            orig_to_tok_index.append(len(all_doc_tokens))
            sub_tokens = tokenizer.tokenize(token)
            for sub_token in sub_tokens:
                tok_to_orig_index.append(i)
                all_doc_tokens.append(sub_token)

        tok_start_position = None
        tok_end_position = None
        if is_training:
            # 没答案或者YES,NO的情况 label在[CLS]位子上
            if example['start_position'] < 0 and example['end_position'] < 0:
                tok_start_position = example['start_position']
                tok_end_position = example['end_position']
            else:  # 有答案的情况下
                tok_start_position = orig_to_tok_index[example['start_position']]  # 原来token到新token的映射，这是新token的起点
                if example['end_position'] < len(example['doc_tokens']) - 1:
                    tok_end_position = orig_to_tok_index[example['end_position'] + 1] - 1
                else:
                    tok_end_position = len(all_doc_tokens) - 1
                (tok_start_position, tok_end_position) = _improve_answer_span(
                    all_doc_tokens, tok_start_position, tok_end_position, tokenizer,
                    example['orig_answer_text'])

        # The -3 accounts for [CLS], [SEP] and [SEP]
        max_tokens_for_doc = max_seq_length - len(query_tokens) - 3

        doc_spans = []
        _DocSpan = collections.namedtuple("DocSpan", ["start", "length"])
        start_offset = 0
        while start_offset < len(all_doc_tokens):
            length = len(all_doc_tokens) - start_offset
            if length > max_tokens_for_doc:
                length = max_tokens_for_doc
            doc_spans.append(_DocSpan(start=start_offset, length=length))
            if start_offset + length == len(all_doc_tokens):
                break
            start_offset += min(length, doc_stride)

        for (doc_span_index, doc_span) in enumerate(doc_spans):
            tokens = []
            token_to_orig_map = {}
            token_is_max_context = {}
            segment_ids = []
            tokens.append("[CLS]")
            segment_ids.append(0)
            for token in query_tokens:
                tokens.append(token)
                segment_ids.append(0)
            tokens.append("[SEP]")
            segment_ids.append(0)

            for i in range(doc_span.length):
                split_token_index = doc_span.start + i
                token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]
                is_max_context = _check_is_max_context(doc_spans, doc_span_index, split_token_index)
                token_is_max_context[len(tokens)] = is_max_context
                tokens.append(all_doc_tokens[split_token_index])
                segment_ids.append(1)
            tokens.append("[SEP]")
            segment_ids.append(1)

            input_ids = tokenizer.convert_tokens_to_ids(tokens)

            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1] * len(input_ids)

            # Zero-pad up to the sequence length.
            while len(input_ids) < max_seq_length:
                input_ids.append(0)
                input_mask.append(0)
                segment_ids.append(0)

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length

            start_position = None
            end_position = None
            target_label = -1  # -1:has answer, 0:unknown, 1:yes, 2:no
            if is_training:
                # For training, if our document chunk does not contain an annotation
                # we throw it out, since there is nothing to predict.
                if tok_start_position < 0 and tok_end_position < 0:
                    start_position = 0  # YES, NO, UNKNOW，0是[CLS]的位子
                    end_position = 0
                    if tok_start_position == -1:  # unknow
                        target_label = 0
                    elif tok_start_position == -2:  # yes
                        target_label = 1
                    elif tok_start_position == -3:  # no
                        target_label = 2
                else:  # 如果原本是有答案的，那么去除没有答案的feature
                    out_of_span = False
                    doc_start = doc_span.start  # 映射回原文的起点和终点
                    doc_end = doc_span.start + doc_span.length - 1

                    if not (tok_start_position >= doc_start and tok_end_position <= doc_end):  # 该划窗没答案作为无答案增强
                        out_of_span = True

                    if out_of_span:
                        start_position = 0
                        end_position = 0
                        target_label = 0
                    else:
                        doc_offset = len(query_tokens) + 2
                        start_position = tok_start_position - doc_start + doc_offset
                        end_position = tok_end_position - doc_start + doc_offset

            features.append({'unique_id': unique_id,
                             'example_index': example_index,
                             'doc_span_index': doc_span_index,
                             'tokens': tokens,
                             'token_to_orig_map': token_to_orig_map,
                             'token_is_max_context': token_is_max_context,
                             'input_ids': input_ids,
                             'input_mask': input_mask,
                             'segment_ids': segment_ids,
                             'start_position': start_position,
                             'end_position': end_position,
                             'target_label': target_label})
            unique_id += 1

    print('features num:', len(features))
    json.dump(features, open(output_files[1], 'w'))
