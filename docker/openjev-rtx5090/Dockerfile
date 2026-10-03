FROM razorback16/openjev@sha256:53741f5005c81e4d19a8a8d66e717213c173bb11a46aac1147eea1dc9244b854

COPY patch_vllm.py /tmp/patch_vllm.py
RUN python /tmp/patch_vllm.py && rm /tmp/patch_vllm.py
