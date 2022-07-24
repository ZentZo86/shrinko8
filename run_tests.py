from utils import *
from unittest.mock import patch

code_file = "timp_p8_tools.py"
code = file_read_text(code_file)
measure_all = "--measure" in sys.argv[1:]
status = 0

def fail_test():
    global status
    status = 1

def run_code(*args):
    try:
        with patch.object(sys, "argv", ["dontcare", *args]):
            exec(code, {"__file__": code_file})
        return True
    except SystemExit:
        print("EXIT!")
        return False
    except Exception:
        traceback.print_exc()
        return False

def measure(kind, path, input=False):
    print("\nMeasuring %s..." % kind)
    if path_exists(path):
        run_code(path, "--input-count" if input else "--count")
    else:
        print("MISSING!")

def run_test(name, input, output, *args, private=False, from_temp=False, to_temp=False):
    prefix = "private_" if private else ""
    inpath = path_join(prefix + ("test_temp" if from_temp else "test_input"), input)
    outpath = path_join(prefix + ("test_temp" if to_temp else "test_output"), output)
    cmppath = path_join(prefix + "test_compare", output)

    success = run_code(inpath, outpath, *args)
    if success and not to_temp:
        success = try_file_read(outpath) == try_file_read(cmppath)

    if not success:
        print("ERROR - test %s failed" % name)
        measure("new", outpath)
        measure("old", cmppath)
        fail_test()
        return False
    elif measure_all and "--minify" in args:
        print("Measuring %s" % name)
        measure("out", outpath)
        measure("in", inpath, input=True)
    return True

def run():
    run_test("minify", "input.p8", "output.p8", "--minify", "--format", "code", 
             "--preserve", "*.preserved_key,preserved_glob,preserving_obj.*",
             "--no-preserve", "circfill,rectfill")
    run_test("semiobfuscate", "input.p8", "output_semiob.p8", "--minify", "--format", "code", 
             "--preserve", "*.*", "--preserve", "preserved_glob",
             "--no-minify-spaces", "--no-minify-lines")
    run_test("test", "test.p8", "test.p8", "--minify")
    run_test("p82png", "test.p8", "testcvt.png", "--format", "png")
    run_test("test_png", "test.png", "test.png", "--minify", "--format", "png")
    run_test("png2p8", "test.png", "testcvt.p8", "--format", "p8")
    if run_test("compress", "test.p8", "testtmp.png", "--format", "png", "--force-compression", to_temp=True):
        run_test("compress_check", "testtmp.png", "test_post_compress.p8", "--format", "p8", from_temp=True)

if __name__ == "__main__":
    os.makedirs("test_output", exist_ok=True)
    os.makedirs("test_temp", exist_ok=True)
    run()

    try:
        from private_run_tests import run as private_run
        private_run()
    except ImportError:
        pass
    
    print("All done")
    sys.exit(status)
