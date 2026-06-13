# First Fundamentals. 

```python

from fastapi import FastAPI

from pydantic import BaseModel

  

# FIRST CREATE A BASIC APP

  
  
  
  

app = FastAPI()

  

@app.get('/get_info')

def get_info():

return {'message':"hello!!!"}
```

```bash
curl -X 'GET' \ 'http://127.0.0.1:8000/get_info' \ -H 'accept: application/json'
```
Use this curl command to send a request.
Understanding the curl command :

1.  **-H 'accept: application/json**  ---> This part was unknown to me , it basically says : format the output data to be a json. , that -H is a header. 

## Passing Inputs to the endpoint

```python(copy)
from fastapi import FastAPI

from pydantic import BaseModel

  


# FIRST CREATE A BASIC APP

  
  
  
  

app = FastAPI()

  

@app.get('/get_info/{Name}')

def get_info(Name):

return {'message':"hello!!! Nice to meet you {Name}"}
```
*Gave ERRORRRR* 

```bash 
curl -X 'GET' \ 'http://127.0.0.1:8000/get_info/Chinmay' \ -H 'accept: application/json'

curl: (3) URL rejected: Malformed input to a URL function

curl: (3) URL rejected: Malformed input to a URL function

curl: (3) URL rejected: Malformed input to a URL function
```

Correct code: 
problems 
1. The fastapi endpoints needs Typehints 
*why* ?? 

### Automatic Data Validation (The Bouncer)

When a user sends data to your API, you can't trust that it's correct. If your endpoint expects an ID number, a user might accidentally send the text `"hello"`.

Because you provide a type hint (like `id: int`), FastAPI automatically acts as a bouncer.

- **Without type hints:** You would have to manually write `if type(id) != int:` checks for every single variable to prevent your code from crashing later.
    
- **With type hints:** FastAPI checks the incoming data _before_ it ever touches your function. If a user sends text instead of an integer, FastAPI automatically blocks the request and replies with a clear error message telling them exactly what they did wrong.


```python 
from fastapi import FastAPI

from pydantic import BaseModel

# FIRST CREATE A BASIC APP

app = FastAPI()

  

@app.get('/get_info/{Name}')

def get_info(Name:str):

return {'message':f"hello!!! Nice to meet you {Name}"}
```

## Passing data using fastapi 

```python

from fastapi import FastAPI

from pydantic import BaseModel

# FIRST CREATE A BASIC APP

app = FastAPI()

  
  

class Person():

def __init__(self , name:str , age:int):

self.name = name

self.age = age

  
  

@classmethod

def create(cls , name:str , age:int):

print("person made")

return cls(name , age)

@app.get('/get_info/{Name}')

def get_info(Name:str):

return {'message':f"hello!!! Nice to meet you {Name}"}

  
  

@app.post('/send_info/{Name}/{age}')

def send_info(Name:str , age:int):

p=Person.create(Name , age)

  

return {

"Name":p.name ,

"Age":p.age}
```

```bash 
curl -X 'POST' \ 'http://127.0.0.1:8000/send_info' \ -H 'accept: application/json' \ -H 'Content-Type: application/json' \ -d '{ "name": "Chinmay", "age": 19 }'
```


**WHY Pydantic??** 

```python 
from fastapi import FastAPI

from pydantic import BaseModel

  

app = FastAPI()

  

# This replaces your entire Person class.

# Pydantic builds the __init__ and data validation behind the scenes!

class Person(BaseModel):

name: str

age: int

  

@app.get('/get_info/{Name}')
def get_info(Name: str):

	return {'message': f"hello!!! Nice to meet you {Name}"}

  

# We remove {Name} and {age} from the URL path.

# By type-hinting 'person: Person', FastAPI knows to expect this data in the JSON body.

@app.post('/send_info')

def send_info(person: Person):

print(f"Person made: {person.name} who is {person.age} years old")

return {"status": "success", "data_received": person}
```

To understand why we use Pydantic here, we have to look at what Python’s built-in standard classes are missing, and how Pydantic fills that gap.

Without Pydantic, handling data sent over the internet turns into an absolute nightmare of manual checking, parsing, and validating. Pydantic automates all of it.

Here are the exact reasons why we swap out a native Python class for a Pydantic `BaseModel` when building APIs:

### 1. Automatic JSON Parsing and Instantiation

When a user sends data to your `/send_info` endpoint, it arrives at your server as a raw JSON string that looks like this:

JSON

```
{"name": "Alice", "age": 25}
```

- **With a standard Python class:** Python does not natively understand JSON. You would have to manually extract the data using something like `data = request.json()`, extract the pieces (`data["name"]`, `data["age"]`), and then pass them into your class constructor: `Person(name=data["name"], age=data["age"])`.
    
- **With Pydantic:** The moment you write `person: Person` in your function arguments, FastAPI passes that raw JSON string directly to Pydantic. Pydantic instantly converts that text into a fully realized Python object. You can immediately call `person.name` or `person.age`.
    

### 2. Deep Data Validation (The Bouncer)

Python is a dynamically typed language. Even if you use type hints on a standard class constructor (`def __init__(self, name: str, age: int)`), Python **does not enforce them at runtime**. A user could pass a string for the age, or a list for the name, and Python will gladly accept it until your code crashes later down the line.

Pydantic **enforces** those types strictly at runtime.

If a client sends this bad data:

JSON

```
{"name": "Alice", "age": "not-a-number"}
```

Pydantic steps in _before_ your function code ever runs, blocks the request, and automatically responds to the user with a highly detailed `422 Unprocessable Entity` error telling them exactly where they messed up:

JSON

```
{
  "detail": [
    {
      "loc": ["body", "age"],
      "msg": "value is not a valid integer",
      "type": "type_error.integer"
    }
  ]
}
```

### 3. Data Cleansing & Coercion (The Smart Translator)

Pydantic doesn't just blindly reject data; it tries to be smart and helpful through a process called **type coercion**.

Imagine a front-end form or a mobile app accidentally sends the age as a string containing digits: `{"name": "Alice", "age": "25"}`.

- A standard Python class will just store `age` as the string `"25"`, which will break your app later if you try to do math or store it in a database integer column.
    
- Pydantic reads your hint (`age: int`), sees the string `"25"`, realizes it can safely be converted into a number, and **automatically casts it** to the actual integer `25` for you.
    

### 4. Zero Boilerplate Code

Look at how much code it takes to build a basic data container using both methods.

**Standard Python Class:**

Python

```
class Person:
    def __init__(self, name: str, age: int):
        self.name = name
        self.age = age
```

_(And if you want to print it cleanly, export it back to JSON, or compare two Person objects, you have to manually write `__repr__`, `__eq__`, and custom serialization methods)._

**Pydantic Model:**

Python

```
class Person(BaseModel):
    name: str
    age: int
```

Because it inherits from `BaseModel`, you instantly get built-in helper utilities out of the box:

- `person.model_dump()` converts the object straight back into a standard Python dictionary.
    
- `person.model_dump_json()` turns the object right back into a JSON string.
    
- Printing it (`print(person)`) automatically yields a beautiful, readable string like `Person(name='Alice', age=25)` without you writing any extra code.
    

### Summary

In short: **FastAPI uses Pydantic as its data-handling engine.** Pydantic ensures that bad data never touches your core logic, valid data is structured perfectly, and your API documentation knows exactly what data format your application expects.


## Basically summarising it ... 

1.  When a user / client sends a request to the server the incoming data is in the form of a JSON. If we hadn't used pydantic here , we would have to mannually deal with fetching relevant info from that json and making a person object. 
2. With pydantic , fastapi looks at the typehint (Person) and calls help from pydantic which uses it;s methods to validate the incoming data with the defined type hints.
3. 