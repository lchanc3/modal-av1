import sys
import modal

if len(sys.argv) != 2:
    sys.exit("用法：python cancel.py <call id>")

modal.FunctionCall.from_id(sys.argv[1]).cancel(terminate_containers=True)
print("已取消", sys.argv[1])
